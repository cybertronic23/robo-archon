#!/usr/bin/env python3
"""Create a custom-package compatibility fixture using OFFICIAL weights, not self-trained weights."""

import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--directory", type=Path, default=ROOT / "tmp-episodes/m2g3-fixture")
args = p.parse_args()
package = args.directory / "policy"
package.mkdir(parents=True, exist_ok=True)
lock = json.loads((ROOT / "robots/microduck.lock.json").read_text())
manifest = {
    "schema_version": 1,
    "id": "example.velstand",
    "version": "0.1.0",
    "body": "microduck",
    "backend": "mujoco",
    "adapter": "microduck.twist.v1",
    "observation_contract": "microduck.proprio_command.61.v1",
    "action_contract": "microduck.joint_offset.14.rad.v1",
    "normalization": "embedded",
    "rate_hz": 50,
    "weight": "policy.onnx",
    "sha256": lock["policy"]["sha256"],
    "source": lock["policy"]["url"]
    + " (official weights copied for compatibility fixture; not self-trained)",
    "license": "Apache-2.0",
}
(package / "policy.json").write_text(json.dumps(manifest, indent=2) + "\n")
shutil.copyfile(
    ROOT / "python/models/external/microduck/velstand.onnx", package / "policy.onnx"
)
skill = json.loads((ROOT / "skills/microduck-walk/skill.json").read_text())
skill.update(
    id="example.walk",
    tool_name="example_walk",
    description="Official-policy custom package compatibility fixture. Forward preset vx=0.4; not a self-trained skill.",
)
skill["bindings"][0]["policy"] = manifest["id"]
directory = args.directory / "skills/official-copy"
directory.mkdir(parents=True, exist_ok=True)
(directory / "skill.json").write_text(json.dumps(skill, indent=2) + "\n")
(args.directory / "call.json").write_text(
    json.dumps(
        {
            "skill_id": "example.walk",
            "parameters": {"vx": 0.4, "duration_ms": 2000},
            "timeout_ms": 4000,
        },
        indent=2,
    )
    + "\n"
)
print(
    json.dumps(
        {
            "policy": str(package),
            "skills": str(args.directory / "skills"),
            "call": str(args.directory / "call.json"),
            "notice": "official weights, not self-trained",
        }
    )
)
