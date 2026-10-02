"""Strict, data-only ONNX policy packages for the installed Microduck adapter."""

import hashlib
import json
import math
from pathlib import Path
import re

CONTRACTS = ("microduck.proprio_command.61.v1", "microduck.joint_offset.14.rad.v1")
JOINTS = "left_hip_yaw,left_hip_roll,left_hip_pitch,left_knee,left_ankle,neck_pitch,head_pitch,head_yaw,head_roll,right_hip_yaw,right_hip_roll,right_hip_pitch,right_knee,right_ankle".split(
    ","
)
POSE = [
    0,
    -0.0873,
    -0.4579,
    -0.0049,
    0.453,
    0.3491,
    0.3491,
    0,
    0,
    0,
    0.0873,
    0.4579,
    0.0049,
    -0.453,
]
OBS = "base_ang_vel,projected_gravity,joint_pos,joint_vel,actions,command,head_command,body_command"
FIELDS = {
    "schema_version",
    "id",
    "version",
    "body",
    "backend",
    "adapter",
    "observation_contract",
    "action_contract",
    "normalization",
    "rate_hz",
    "weight",
    "sha256",
    "source",
    "license",
}


def validate(package):
    import numpy as np
    import onnxruntime as ort

    package = Path(package)
    if package.is_symlink() or (package / "policy.json").is_symlink():
        raise ValueError("policy symlinks unsupported")
    manifest = json.loads((package / "policy.json").read_text())
    if (
        set(manifest) != FIELDS
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
    ):
        raise ValueError("unsupported policy manifest fields/version")
    if (
        not isinstance(manifest["id"], str)
        or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", manifest["id"])
        or manifest["id"] == "velstand"
        or manifest["id"].startswith("official.")
    ):
        raise ValueError("invalid/reserved policy ID")
    for key in ["version", "source", "license"]:
        if not isinstance(manifest[key], str) or not manifest[key].strip():
            raise ValueError(f"{key} must be nonempty")
    if (manifest["body"], manifest["backend"], manifest["adapter"]) != (
        "microduck",
        "mujoco",
        "microduck.twist.v1",
    ):
        raise ValueError("adapter/body/backend unsupported")
    if (
        (manifest["observation_contract"], manifest["action_contract"]) != CONTRACTS
        or manifest["normalization"] != "embedded"
        or type(manifest["rate_hz"]) not in (int, float)
        or manifest["rate_hz"] != 50
    ):
        raise ValueError("policy contracts/normalization/rate mismatch")
    filename = manifest["weight"]
    if not isinstance(filename, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+\.onnx", filename
    ):
        raise ValueError("weight must be a local ONNX basename")
    weight = package / filename
    if weight.is_symlink() or not weight.is_file():
        raise ValueError("weight must be a regular file")
    digest = hashlib.sha256(weight.read_bytes()).hexdigest()
    if digest != manifest["sha256"]:
        raise ValueError("policy weight SHA-256 mismatch")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(weight), sess_options=options, providers=["CPUExecutionProvider"]
    )
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if (
        len(inputs) != 1
        or len(outputs) != 1
        or inputs[0].shape != [1, 61]
        or outputs[0].shape != [1, 14]
        or inputs[0].type != "tensor(float)"
        or outputs[0].type != "tensor(float)"
    ):
        raise ValueError("ONNX requires float32 [1,61] -> [1,14]")
    metadata = session.get_modelmeta().custom_metadata_map
    if (
        metadata.get("joint_names", "").split(",") != JOINTS
        or metadata.get("observation_names") != OBS
        or metadata.get("command_names") != "twist,head_pose,body_pose"
    ):
        raise ValueError("ONNX joint order/observation/command metadata mismatch")
    if metadata.get("action_scale") != "1.0":
        raise ValueError("ONNX action scale mismatch")
    pose = [float(v) for v in metadata.get("default_joint_pos", "").split(",")]
    if len(pose) != 14 or any(
        not math.isfinite(v) or abs(v - p) > 0.001 for v, p in zip(pose, POSE)
    ):
        raise ValueError("ONNX default pose mismatch")
    # An export may reference external tensors; copying only this ONNX must remain self-contained.
    trial = session.run(
        [outputs[0].name], {inputs[0].name: np.zeros((1, 61), dtype=np.float32)}
    )[0]
    if trial.shape != (1, 14) or not np.isfinite(trial).all():
        raise ValueError("ONNX smoke inference non-finite/wrong shape")
    return manifest, weight


def verify_installed(package):
    manifest, weight = validate(package)
    receipt_path = Path(package) / ".archon-policy.json"
    if receipt_path.is_symlink():
        raise ValueError("policy receipt symlinks unsupported")
    receipt = json.loads(receipt_path.read_text())
    if receipt != manifest:
        raise ValueError("policy install receipt mismatch")
    return manifest, weight
