"""Offline runtime URDF fingerprint/version checks without importing SAPIEN."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python/robo_archon_sim_workers"))
from asset_integrity import tree_hash, validate_runtime_asset


class RuntimeAssetTests(unittest.TestCase):
    def test_version_and_content_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            folder = base / "robots/panda"
            folder.mkdir(parents=True)
            urdf = folder / "panda_v2.urdf"
            urdf.write_text("<robot/>")
            lock = base / "lock.json"
            asset = dict(
                directory="robots/panda",
                sha256=tree_hash(folder),
                runtime_versions={"fixture": "1.0"},
            )
            lock.write_text(
                json.dumps(dict(schema_version=1, assets={"maniskill_panda": asset}))
            )
            with patch("importlib.metadata.version", return_value="1.0+cpu"):
                self.assertEqual(validate_runtime_asset(lock, base), asset)
                urdf.write_text("<changed/>")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    validate_runtime_asset(lock, base)
            with patch("importlib.metadata.version", return_value="2.0"):
                with self.assertRaisesRegex(ValueError, "version mismatch"):
                    validate_runtime_asset(lock, base)


if __name__ == "__main__":
    unittest.main()
