#!/usr/bin/env python3
"""Install checksum-pinned official behavior weights outside Git."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/robo_archon_sim_workers"))
from official_behaviors import DEFAULT, locked, verify  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path)
    p.add_argument("--destination", type=Path, default=DEFAULT)
    args = p.parse_args()
    lock = locked()
    if args.destination.exists():
        verify(args.destination)
        print("Official behaviors already verified")
        return
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=args.destination.parent, prefix=".official-behaviors-"
    ) as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir()
        for entry in lock["behaviors"].values():
            file = stage / entry["file"]
            if args.source:
                shutil.copyfile(args.source / entry["file"], file)
            else:
                url = (
                    lock["repository"]
                    + "/resolve/"
                    + lock["revision"]
                    + "/"
                    + entry["file"]
                )
                with urllib.request.urlopen(url, timeout=30) as response:
                    file.write_bytes(response.read())
            if hashlib.sha256(file.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("download checksum mismatch")
        (stage / "receipt.json").write_text(json.dumps(lock, indent=2) + "\n")
        verify(stage)
        stage.rename(args.destination)
    print("Official behaviors installed and verified")


if __name__ == "__main__":
    main()
