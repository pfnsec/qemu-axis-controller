import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import board
import framefx
import sdcard

SPEC = importlib.util.spec_from_file_location("board_probe", board.PAYLOAD / "board_probe.py")
board_probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(board_probe)
DISPLAY_SPEC = importlib.util.spec_from_file_location("board_display", board.PAYLOAD / "display.py")
board_display = importlib.util.module_from_spec(DISPLAY_SPEC)
DISPLAY_SPEC.loader.exec_module(board_display)


def removable_disk():
    return {"path": "/dev/sda", "maj:min": "8:0", "size": 16 * 2**30, "model": "Test reader",
            "serial": "fixture-only", "tran": "usb", "rm": True, "ro": False, "type": "disk",
            "mountpoints": [None], "children": [{"path": "/dev/sda1", "type": "part", "mountpoints": [None]}]}


class TargetSafetyTests(unittest.TestCase):
    def test_explicit_unmounted_removable_disk(self):
        identity = sdcard.validate_target([removable_disk()], "/dev/sda", 4096)
        self.assertEqual(identity["serial"], "fixture-only")
        self.assertEqual(len(sdcard.identity_token(identity)), 64)
        self.assertNotEqual(sdcard.identity_token(identity), sdcard.identity_token({**identity, "serial": "replacement"}))

    def test_wrong_disk_types_and_incomplete_identity_rejected(self):
        for replacement in ({"type": "part"}, {"type": "loop"}, {"rm": False}, {"rm": 1},
                            {"ro": True}, {"tran": "nvme"}, {"serial": ""}, {"model": None},
                            {"size": 100}, {"size": 257 * 2**30}, {"size": "16000000000"}):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                sdcard.validate_target([{**removable_disk(), **replacement}], "/dev/sda", 4096)
        for target in ("/dev/sda1", "/dev/nvme0n1", "/dev/missing"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                sdcard.validate_target([removable_disk()], target, 4096)
        with self.assertRaises(ValueError):
            sdcard.validate_target([removable_disk(), removable_disk()], "/dev/sda", 4096)

    def test_mounted_swap_and_mapped_descendants_rejected(self):
        for mountpoint in ("/", "/run/media/test", "[SWAP]"):
            disk = removable_disk()
            disk["children"][0]["mountpoints"] = [mountpoint]
            with self.subTest(mountpoint=mountpoint), self.assertRaises(ValueError):
                sdcard.validate_target([disk], "/dev/sda", 4096)
        disk = removable_disk()
        disk["children"][0]["children"] = [{"type": "crypt", "mountpoints": []}]
        with self.assertRaises(ValueError):
            sdcard.validate_target([disk], "/dev/sda", 4096)

    def test_inspect_rejects_ordinary_files(self):
        with tempfile.NamedTemporaryFile() as ordinary:
            with self.assertRaisesRegex(ValueError, "not a block device"):
                sdcard.inspect(ordinary.name, 4096)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.data = bytes(range(256)) * 32
        self.digest = hashlib.sha256(self.data).hexdigest()

    def test_complete_hash_and_partial_writes(self):
        class PartialWriter(io.BytesIO):
            def write(self, data):
                return super().write(data[:7])

        destination = PartialWriter()
        sdcard.copy_image(io.BytesIO(self.data), destination, len(self.data), self.digest)
        self.assertEqual(destination.getvalue(), self.data)
        self.assertEqual(sdcard.digest_prefix(io.BytesIO(self.data + b"tail"), len(self.data)), self.digest)

    def test_truncation_hash_mismatch_and_append_rejected(self):
        for data in (self.data[:-1], self.data + b"extra", bytes(len(self.data))):
            with self.subTest(length=len(data)), self.assertRaises(RuntimeError):
                sdcard.copy_image(io.BytesIO(data), io.BytesIO(), len(self.data), self.digest)
        with self.assertRaises(RuntimeError):
            sdcard.digest_prefix(io.BytesIO(self.data[:-1]), len(self.data))
        for length in (-1, 0, 1.5, True):
            with self.subTest(length=length), self.assertRaises(ValueError):
                sdcard.digest_prefix(io.BytesIO(self.data), length)

    def test_failed_device_write_rejected(self):
        for result in (0, None):
            destination = mock.Mock()
            destination.write.return_value = result
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                sdcard.copy_image(io.BytesIO(self.data), destination, len(self.data), self.digest)

    def test_bad_source_or_changed_identity_never_opens_device(self):
        identity = sdcard.validate_target([removable_disk()], "/dev/sda", len(self.data))
        for failure in ("source", "identity"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                image = Path(temporary) / "image.img"
                evidence = Path(temporary) / "write.json"
                image.write_bytes(self.data)
                identities = [identity, {**identity, "serial": "replaced"}]
                with mock.patch.object(sdcard.os, "geteuid", return_value=0), \
                     mock.patch.object(sdcard, "inspect", side_effect=identities), \
                     mock.patch.object(sdcard, "open_device") as device_open:
                    with self.assertRaises(ValueError):
                        sdcard.flash(image, "/dev/sda", "0" * 64 if failure == "source" else self.digest,
                                     sdcard.identity_token(identity), evidence)
                    device_open.assert_not_called()
                self.assertEqual(json.loads(evidence.read_text())["status"], "FAIL")

    def test_mock_device_readback_is_required_for_pass(self):
        class DeviceBuffer(io.BytesIO):
            def fileno(self):
                return 123

        identity = sdcard.validate_target([removable_disk()], "/dev/sda", len(self.data))
        identity["size"] = 2**20 + len(self.data)
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temporary:
                image = Path(temporary) / "image.img"
                evidence = Path(temporary) / "write.json"
                image.write_bytes(self.data)
                destination = DeviceBuffer(bytes(identity["size"]))

                def ioctl(descriptor, request, *arguments):
                    if request == 0x80081272:
                        return struct.pack("Q", identity["size"])
                    self.assertEqual(request, 0x1261)
                    if corrupt:
                        destination.seek(0)
                        destination.write(b"x")

                with mock.patch.object(sdcard.os, "geteuid", return_value=0), \
                     mock.patch.object(sdcard, "inspect", return_value=identity), \
                     mock.patch.object(sdcard, "open_device", return_value=destination), \
                     mock.patch.object(sdcard.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 0))), \
                     mock.patch.object(sdcard.os, "fsync"), mock.patch.object(sdcard.fcntl, "ioctl", side_effect=ioctl):
                    if corrupt:
                        with self.assertRaisesRegex(RuntimeError, "readback"):
                            sdcard.flash(image, "/dev/sda", self.digest, sdcard.identity_token(identity), evidence)
                    else:
                        sdcard.flash(image, "/dev/sda", self.digest, sdcard.identity_token(identity), evidence)
                record = json.loads(evidence.read_text())
                self.assertEqual(record["status"], "FAIL" if corrupt else "PASS")
                self.assertEqual(record["physical_boot"], "NOT_TESTED")


class ImagePackagingTests(unittest.TestCase):
    def test_desktop_home_repair_is_numeric_nonrecursive_and_verified(self):
        account = "xilinx:x:1000:1000:Xilinx User:/home/xilinx:/bin/bash\n"
        expected = f"{stat.S_IFDIR | 0o750:x}:1000:1000"
        for original in (f"{stat.S_IFDIR | 0o750:x}:127:135", expected):
            with self.subTest(original=original), \
                 mock.patch.object(board, "inside", side_effect=lambda root, name, **kwargs: root / name), \
                 mock.patch.object(board.subprocess, "check_output", side_effect=[account, original, expected]), \
                 mock.patch.object(board.lab, "run") as run:
                root = Path("/image-fixture")
                result = board.prepare_desktop_home(root)
                self.assertEqual(result, dict(path="/home/xilinx", before=original, after=expected))
                run.assert_called_once_with(["sudo", "-n", "chown", "--no-dereference", "1000:1000", root / "home/xilinx"])

    def test_desktop_home_repair_rejects_changed_accounts_and_metadata(self):
        account = "xilinx:x:1000:1000:Xilinx User:/home/xilinx:/bin/bash\n"
        invalid_accounts = ("", account + account, account.replace(":1000:", ":0:", 1),
                            account.replace("/home/xilinx", "/root"), "xilinx:incomplete\n")
        responses = [[entry] for entry in invalid_accounts]
        responses.extend([account, metadata] for metadata in (
            f"{stat.S_IFLNK | 0o777:x}:127:135", f"{stat.S_IFDIR | 0o777:x}:127:135",
            f"{stat.S_IFDIR | 0o750:x}:0:0"))
        for response in responses:
            with self.subTest(response=response), \
                 mock.patch.object(board, "inside", side_effect=lambda root, name, **kwargs: root / name), \
                 mock.patch.object(board.subprocess, "check_output", side_effect=response), \
                 mock.patch.object(board.lab, "run") as run:
                with self.assertRaises(RuntimeError):
                    board.prepare_desktop_home(Path("/image-fixture"))
                run.assert_not_called()

    def test_desktop_home_repair_requires_readback(self):
        account = "xilinx:x:1000:1000:Xilinx User:/home/xilinx:/bin/bash\n"
        original = f"{stat.S_IFDIR | 0o750:x}:127:135"
        with mock.patch.object(board, "inside", side_effect=lambda root, name, **kwargs: root / name), \
             mock.patch.object(board.subprocess, "check_output", side_effect=[account, original, original]), \
             mock.patch.object(board.lab, "run"):
            with self.assertRaisesRegex(RuntimeError, "did not verify"):
                board.prepare_desktop_home(Path("/image-fixture"))

    def test_partition_geometry_is_pinned(self):
        lock = json.loads(board.LOCK_FILE.read_text())
        table = {"label": "dos", "unit": "sectors", "sectorsize": 512, "partitions": copy.deepcopy(lock["partitions"])}
        board.validate_partitions(table, lock)
        for key, value in (("label", "gpt"), ("sectorsize", 4096), ("unit", "bytes"), ("partitions", [])):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                board.validate_partitions({**table, key: value}, lock)
        table["partitions"][1]["start"] += 1
        with self.assertRaises(RuntimeError):
            board.validate_partitions(table, lock)

    def test_payload_cannot_follow_symlinks_outside_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "escape").symlink_to("/etc")
            self.assertEqual(board.inside(root, "opt/axi-lab/probe.py"), root / "opt/axi-lab/probe.py")
            for relative in ("escape/passwd", "../outside", "/etc/passwd"):
                with self.subTest(relative=relative), self.assertRaises(RuntimeError):
                    board.inside(root, relative)

    def test_missing_or_stale_image_is_not_current(self):
        self.assertFalse(board.evidence_current({"status": "PASS"}))
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            image = Path(temporary) / "fixture.img"
            image.write_bytes(b"fixture")
            record = dict(status="PASS", stage="H0-image", image=str(image.relative_to(ROOT)), image_bytes=7,
                          source_sha256=board.image_inputs(), image_sha256=hashlib.sha256(b"fixture").hexdigest())
            self.assertTrue(board.evidence_current(record))
            image.write_bytes(b"changed")
            self.assertFalse(board.evidence_current(record))


class ProbeTests(unittest.TestCase):
    def test_diagnostics_never_claim_visible_display_or_pl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contents = {"proc/device-tree/model": "Avnet Ultra96 Rev1\x00",
                        "proc/device-tree/compatible": "avnet,ultra96-rev1\x00xlnx,zynqmp\x00",
                        "proc/sys/kernel/osrelease": "fixture-kernel",
                        "proc/meminfo": "MemTotal: 1000 kB\nCmaFree: 900 kB\nIrrelevant: 50 kB\n",
                        "sys/class/drm/card0-DP-1/status": "connected\n",
                        "sys/class/drm/card0-DP-1/modes": "1920x1080\n1280x720\n"}
            for filename, content in contents.items():
                target = root / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            report = board_probe.collect(root)
            self.assertEqual(report["board_identity"], "MATCH")
            self.assertEqual(report["connectors"][0]["modes"], ["1920x1080", "1280x720"])
            self.assertNotIn("Irrelevant", report["memory"])
            self.assertEqual(report["display_visible"], "REQUIRES_OPERATOR_OBSERVATION")
            self.assertEqual(report["framefx"], "NOT_LOADED_NOT_TESTED")
            self.assertEqual(report["physical_dma"], "NOT_TESTED")
            (root / "proc/device-tree/compatible").write_text("avnet,ultra96-v2\x00")
            self.assertEqual(board_probe.collect(root)["board_identity"], "UNVERIFIED")

    def test_missing_files_and_bounded_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(board_probe.collect(root)["board_identity"], "UNVERIFIED")
            (root / "large").write_bytes(b"x" * 10000)
            self.assertEqual(board_probe.read_text(root, "large", limit=50), "x" * 50)


class NativeReferenceTests(unittest.TestCase):
    def test_native_reference_matches_simulation(self):
        generator = random.Random(9602)
        for width, height in ((1, 1), (3, 3), (7, 5), (65, 9), (320, 240)):
            pixels = generator.randbytes(width * height)
            for mode in range(4):
                if mode == 3 and min(width, height) < 3:
                    continue
                for threshold in (0, 127, 255):
                    with self.subTest(width=width, height=height, mode=mode, threshold=threshold):
                        self.assertEqual(board_display.transform(pixels, width, height, mode, threshold),
                                         framefx.transform(pixels, width, height, mode, threshold))

    def test_pattern_is_animated_and_bounded(self):
        first = board_display.pattern(320, 240, 0)
        second = board_display.pattern(320, 240, 9)
        self.assertEqual(len(first), 320 * 240)
        self.assertNotEqual(first, second)
        self.assertEqual(first, board_display.pattern(320, 240, 0))

    def test_invalid_geometry_rejected(self):
        for arguments in ((b"", 0, 0, 0), (b"x", 1, 1, 3), (b"x", 1, 1, 4), (b"x", 1, 1, 0, 256)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                board_display.transform(*arguments)


@unittest.skipUnless(shutil.which("node"), "optional H0 browser-reference checks require Node.js")
class BrowserReferenceTests(unittest.TestCase):
    def test_javascript_matches_independent_python_reference(self):
        cases = []
        expected = []
        generator = random.Random(9601)
        for width, height in ((1, 1), (3, 3), (7, 5), (65, 9), (320, 240)):
            pixels = generator.randbytes(width * height)
            for mode in range(4):
                if mode == 3 and min(width, height) < 3:
                    continue
                for threshold in (0, 127, 255):
                    cases.append(dict(source=pixels.hex(), width=width, height=height, mode=mode, threshold=threshold))
                    expected.append(hashlib.sha256(framefx.transform(pixels, width, height, mode, threshold)).hexdigest())
        script = """
const reference = require(process.argv[1]);
const crypto = require('crypto');
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(test => crypto.createHash('sha256').update(reference.transform(
    Buffer.from(test.source, 'hex'), test.width, test.height, test.mode, test.threshold)).digest('hex'))));
"""
        result = subprocess.run(["node", "-e", script, str(board.PAYLOAD / "reference.js")],
                                input=json.dumps(cases), text=True, capture_output=True, check=True, timeout=20)
        self.assertEqual(json.loads(result.stdout), expected)


if __name__ == "__main__":
    unittest.main()
