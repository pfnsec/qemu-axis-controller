import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pl_bundle


class PlBundleTests(unittest.TestCase):
    def test_native_inputs_are_exact_current_and_not_simulator_modules(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(pl_bundle.lab, "ROOT", Path(temporary)):
            directory = Path(temporary) / "native"
            directory.mkdir()
            for name in pl_bundle.NATIVE_SOURCES:
                path = Path(temporary) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"source")
            for name in pl_bundle.NATIVE_ARTIFACTS:
                (directory / name).write_bytes(b"artifact")
            record = dict(schema=1, stage="native-build-only", kernel=pl_bundle.pl.PLATFORM["kernel"],
                          source_sha256={name: pl_bundle.lab.sha256(Path(temporary) / name) for name in pl_bundle.NATIVE_SOURCES},
                          artifact_sha256={name: pl_bundle.lab.sha256(directory / name) for name in pl_bundle.NATIVE_ARTIFACTS})
            pl_bundle.validate_native(record, directory)
            for changed in ({"kernel": "6.12.109"}, {"source_sha256": {}}, {"artifact_sha256": {}}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    pl_bundle.validate_native({**record, **changed}, directory)
            (Path(temporary) / "linux/framefx/driver/framefx.c").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                pl_bundle.validate_native(record, directory)

    def test_stale_pl_build_prevents_bundle_creation(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(pl_bundle.lab, "ARTIFACTS", Path(temporary)), \
             mock.patch.object(pl_bundle.pl, "evidence_current", return_value=False):
            (Path(temporary) / "ultra96-pl-bitstream.json").write_text(json.dumps({"status": "PASS"}))
            with self.assertRaises(ValueError):
                pl_bundle.assemble(Path(temporary) / "native.json")
            self.assertFalse((Path(temporary) / "hardware").exists())


if __name__ == "__main__":
    unittest.main()
