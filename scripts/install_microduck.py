#!/usr/bin/env python3
"""Install a pinned official Microduck simulation package without executing upstream code."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/robo_archon_sim_workers"))
from asset_integrity import tree_hash  # noqa: E402 — repository bootstrap path


def verify(package, lock):
    stamp = json.loads((package / ".robo-archon-install.json").read_text())
    if stamp["lock"] != lock or stamp["sha256"] != tree_hash(package):
        raise ValueError(
            "Microduck install fingerprint/version mismatch; preserve it before reinstalling"
        )
    if (
        hashlib.sha256((package / "velstand.onnx").read_bytes()).hexdigest()
        != lock["policy"]["sha256"]
    ):
        raise ValueError("Microduck policy checksum mismatch")
    return stamp


def checkout(entry, seed, temporary):
    source = seed
    if source is None:
        source = temporary
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "fetch",
                "--depth",
                "1",
                entry["repository"],
                entry["revision"],
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(source), "checkout", "--detach", "-q", "FETCH_HEAD"],
            check=True,
        )
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != entry["revision"]:
        raise ValueError("seed source revision differs from lock")
    subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD"], check=True)
    return source


def export_tracked(source, paths, destination, archive):
    # Seed checkouts may contain ignored/untracked files: export only pinned Git content.
    with archive.open("wb") as stream:
        subprocess.run(
            ["git", "-C", str(source), "archive", "--format=tar", "HEAD", *paths],
            stdout=stream,
            check=True,
        )
    destination.mkdir()
    with tarfile.open(archive) as stream:
        if any(member.issym() or member.islnk() for member in stream.getmembers()):
            raise ValueError("symlink assets unsupported")
        stream.extractall(destination, filter="data")


def install(destination, lock, rl_source=None, bam_source=None, policy_source=None):
    if destination.exists():
        verify(destination, lock)
        print(f"Microduck already installed and verified: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="microduck-install-", dir=destination.parent
    ) as td:
        temporary = Path(td)
        rl = checkout(lock["rl"], rl_source, temporary / "rl-source")
        bam = checkout(lock["bam"], bam_source, temporary / "bam-source")
        export_tracked(
            rl,
            [
                "src/mjlab_microduck/robot/microduck",
                "scripts/infer_policy.py",
                "LICENSE",
                "README.md",
            ],
            temporary / "rl-clean",
            temporary / "rl.tar",
        )
        export_tracked(
            bam, ["bam", "LICENSE"], temporary / "bam-clean", temporary / "bam.tar"
        )
        rl, bam = temporary / "rl-clean", temporary / "bam-clean"
        package = temporary / "package"
        package.mkdir()
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")
        shutil.copytree(
            rl / "src/mjlab_microduck/robot/microduck", package / "model", ignore=ignore
        )
        shutil.copytree(bam / "bam", package / "bam", ignore=ignore)
        shutil.copy2(rl / "scripts/infer_policy.py", package / "official_infer.py")
        shutil.copy2(rl / "LICENSE", package / "RL-LICENSE")
        shutil.copy2(rl / "README.md", package / "RL-README.md")
        shutil.copy2(bam / "LICENSE", package / "BAM-LICENSE")
        (package / "ATTRIBUTION.json").write_text(json.dumps(lock, indent=2) + "\n")
        if policy_source:
            shutil.copy2(policy_source, package / "velstand.onnx")
        else:
            with urllib.request.urlopen(lock["policy"]["url"], timeout=120) as response:
                (package / "velstand.onnx").write_bytes(response.read())
        if (
            hashlib.sha256((package / "velstand.onnx").read_bytes()).hexdigest()
            != lock["policy"]["sha256"]
        ):
            raise ValueError("official policy checksum mismatch")
        if any(p.is_symlink() for p in package.rglob("*")):
            raise ValueError("symlink assets unsupported")
        stamp = {"lock": lock, "sha256": tree_hash(package)}
        (package / ".robo-archon-install.json").write_text(
            json.dumps(stamp, indent=2) + "\n"
        )
        package.rename(destination)
    print(f"Microduck installed: {destination}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination", type=Path, default=ROOT / "python/models/external/microduck"
    )
    parser.add_argument("--rl-source", type=Path)
    parser.add_argument("--bam-source", type=Path)
    parser.add_argument("--policy-source", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    lock = json.loads((ROOT / "robots/microduck.lock.json").read_text())
    if args.verify:
        verify(args.destination, lock)
        print("Microduck package verified")
    else:
        install(
            args.destination, lock, args.rl_source, args.bam_source, args.policy_source
        )


if __name__ == "__main__":
    main()
