#!/usr/bin/env python3
"""Runtime readiness only; structural compatibility does not certify a learned behavior."""

import argparse
import importlib.metadata
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/robo_archon_sim_workers"))
from policy_packages import verify_installed  # noqa: E402
from install_microduck import verify  # noqa: E402

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--packages", type=Path, required=True)
    args = p.parse_args()
    lock = json.loads((ROOT / "robots/microduck.lock.json").read_text())
    ready = []
    rejected = []
    try:
        verify(args.assets, lock)
        if sys.version_info[:2] != (3, 12):
            raise ValueError("Python 3.12 required")
        for name, version in lock["runtime_versions"].items():
            if importlib.metadata.version(name) != version:
                raise ValueError(f"{name} version mismatch")
    except Exception as e:
        print(
            json.dumps({"ready": [], "rejected": [{"id": "runtime", "reason": str(e)}]})
        )
        sys.exit(0)
    ready.append({"id": "velstand", "package": None, "manifest": None})
    from official_behaviors import DEFAULT, profiles
    if DEFAULT.exists():
        try:
            ready.extend(profiles())
        except Exception as e:
            rejected.append({"id":"official_behaviors","reason":str(e)})
    if args.packages.exists():
        for package in sorted(args.packages.iterdir()):
            if not package.is_dir() or package.name.startswith("."):
                continue
            try:
                m, _ = verify_installed(package)
                if package.name != m["id"]:
                    raise ValueError("installed directory/ID mismatch")
                ready.append(
                    {"id": m["id"], "package": str(package.resolve()), "manifest": m}
                )
            except Exception as e:
                rejected.append({"id": package.name, "reason": str(e)})
    print(json.dumps({"ready": ready, "rejected": rejected}))
