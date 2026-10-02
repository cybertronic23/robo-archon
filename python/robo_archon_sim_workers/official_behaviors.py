"""Pinned, trusted official behavior adapters; distinct from velocity-command packages."""

import hashlib
import json
import math
from pathlib import Path
from policy_packages import JOINTS, OBS, POSE, CONTRACTS

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "python/policies/official-microduck"


def locked():
    return json.loads((ROOT / "robots/microduck-behaviors.lock.json").read_text())


def verify(directory=DEFAULT):
    import numpy as np
    import onnxruntime as ort

    directory = Path(directory)
    lock = locked()
    if (
        directory.is_symlink()
        or (directory / "receipt.json").is_symlink()
        or json.loads((directory / "receipt.json").read_text()) != lock
    ):
        raise ValueError("official behaviors receipt mismatch")
    for name, entry in lock["behaviors"].items():
        path = directory / entry["file"]
        if (
            path.is_symlink()
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]
        ):
            raise ValueError(f"official behavior checksum mismatch: {name}")
        s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        i, o = s.get_inputs(), s.get_outputs()
        m = s.get_modelmeta().custom_metadata_map
        pose = [float(v) for v in m.get("default_joint_pos", "").split(",")]
        if (
            len(i) != 1
            or len(o) != 1
            or i[0].shape != [1, 61]
            or o[0].shape != [1, 14]
            or i[0].type != "tensor(float)"
            or o[0].type != "tensor(float)"
            or m.get("joint_names", "").split(",") != JOINTS
            or m.get("observation_names") != OBS
            or m.get("command_names") != entry["command_names"]
            or m.get("action_scale") != "1.0"
            or len(pose) != 14
            or any(
                not math.isfinite(v) or abs(v - p) > 0.001 for v, p in zip(pose, POSE)
            )
        ):
            raise ValueError(f"official behavior contract mismatch: {name}")
        if not np.isfinite(
            s.run(None, {i[0].name: np.zeros((1, 61), dtype=np.float32)})[0]
        ).all():
            raise ValueError("nonfinite official behavior inference")
    return lock


def profiles(directory=DEFAULT):
    lock = verify(directory)
    return [
        {
            "id": "official." + name,
            "package": None,
            "manifest": {
                "id": "official." + name,
                "observation_contract": CONTRACTS[0],
                "action_contract": CONTRACTS[1],
                "source": lock["repository"]
                + "/resolve/"
                + lock["revision"]
                + "/"
                + entry["file"],
                "sha256": entry["sha256"],
                "license": lock["license"],
            },
        }
        for name, entry in lock["behaviors"].items()
    ]
