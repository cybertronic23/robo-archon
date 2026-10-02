#!/usr/bin/env python3
"""Install a compatible ONNX package; no scripts, URLs or pickle weights execute."""

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/robo_archon_sim_workers"))
from policy_packages import validate, verify_installed  # noqa: E402


def install(source, destination_root):
    manifest, weight = validate(source)
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / manifest["id"]
    if destination.exists():
        previous, _ = verify_installed(destination)
        if previous != manifest:
            raise ValueError(
                "existing policy differs; use a new ID or preserve/remove it explicitly"
            )
        return destination
    with tempfile.TemporaryDirectory(dir=destination_root, prefix=".install-") as tmp:
        staged = Path(tmp) / "package"
        staged.mkdir()
        (staged / "policy.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.copyfile(weight, staged / manifest["weight"])
        # Validate copied contents as well: external tensor files and changing sources fail here.
        validate(staged)
        (staged / ".archon-policy.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        staged.rename(destination)
    return destination


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument(
        "--destination-root", type=Path, default=ROOT / "python/policies/external"
    )
    args = p.parse_args()
    print(json.dumps({"installed": str(install(args.source, args.destination_root))}))
