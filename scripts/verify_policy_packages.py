#!/usr/bin/env python3
"""Package failure-mode tests; fixture uses official weights, never pretends to be trained."""

import json
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "python/robo_archon_sim_workers"))
from install_policy import install  # noqa: E402
from policy_packages import validate, verify_installed  # noqa: E402

if __name__ == "__main__":
    fixture = ROOT / "tmp-episodes/m2g3-fixture/policy"
    manifest = json.loads((fixture / "policy.json").read_text())
    checks = []
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "source"
        shutil.copytree(fixture, source)
        installed = install(source, Path(td) / "installed")
        assert install(source, Path(td) / "installed") == installed
        verify_installed(installed)
        checks.extend(["clean install", "idempotence"])
        for key, value in [
            ("sha256", "0" * 64),
            ("weight", "../policy.onnx"),
            ("rate_hz", 100),
            ("action_contract", "wrong"),
            ("normalization", "external"),
            ("adapter", "shell"),
            ("id", "velstand"),
            ("unexpected", True),
        ]:
            altered = {**manifest, key: value}
            (source / "policy.json").write_text(json.dumps(altered))
            try:
                validate(source)
            except (ValueError, RuntimeError):
                checks.append(f"reject {key}")
            else:
                raise AssertionError(f"accepted invalid {key}")
        (source / "policy.json").write_text(json.dumps(manifest))
        # Alter actual ONNX metadata without changing protobuf string length.
        original = (source / "policy.onnx").read_bytes()
        from policy_packages import JOINTS

        names = ",".join(JOINTS).encode()
        swapped = ",".join([JOINTS[1], JOINTS[0], *JOINTS[2:]]).encode()
        assert len(names) == len(swapped) and original.count(names) == 1
        corrupted = original.replace(names, swapped)
        (source / "policy.onnx").write_bytes(corrupted)
        (source / "policy.json").write_text(
            json.dumps({**manifest, "sha256": hashlib.sha256(corrupted).hexdigest()})
        )
        try:
            validate(source)
        except ValueError as error:
            assert "joint order" in str(error)
            checks.append("reject actual ONNX joint-order mismatch with valid checksum")
        else:
            raise AssertionError("accepted reordered joint metadata")
        (source / "policy.onnx").write_bytes(original)
        changed = {**manifest, "version": "other"}
        (source / "policy.json").write_text(json.dumps(changed))
        try:
            install(source, Path(td) / "installed")
        except ValueError:
            checks.append("refuse overwrite")
        else:
            raise AssertionError("overwrote existing package")
        (installed / "policy.onnx").write_bytes(b"tamper")
        try:
            verify_installed(installed)
        except ValueError:
            checks.append("installed tamper")
        else:
            raise AssertionError("accepted tamper")
    output = ROOT / "tmp-episodes/m2g3-fixture/package-acceptance.json"
    output.write_text(
        json.dumps(
            {
                "acceptance": "passed",
                "checks": checks,
                "weights": "official compatibility fixture, not self-trained",
            },
            indent=2,
        )
        + "\n"
    )
    print("Policy package checks passed")
