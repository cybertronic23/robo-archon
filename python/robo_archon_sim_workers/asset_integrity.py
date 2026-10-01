"""Content fingerprint of installed model files (not a publisher signature)."""

import hashlib


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.name != ".robo-archon-install.json"
    ):
        if path.is_symlink():
            raise ValueError("asset symlinks are not supported")
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def validate_runtime_asset(lock_path, package_asset_dir):
    """Verify bundled URDF/meshes against our committed runtime version and fingerprint."""
    import importlib.metadata
    import json
    from pathlib import Path

    lock = json.loads(Path(lock_path).read_text())
    if lock["schema_version"] != 1:
        raise ValueError("unsupported runtime asset lock")
    asset = lock["assets"]["maniskill_panda"]
    for package, version in asset["runtime_versions"].items():
        if importlib.metadata.version(package).split("+")[0] != version:
            raise ValueError(f"runtime version mismatch: {package} requires {version}")
    root = Path(package_asset_dir) / asset["directory"]
    if tree_hash(root) != asset["sha256"]:
        raise ValueError(
            "ManiSkill Panda asset checksum mismatch; reinstall pinned environment"
        )
    return asset
