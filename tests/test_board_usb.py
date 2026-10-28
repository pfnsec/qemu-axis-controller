import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import board_usb

SPEC = importlib.util.spec_from_file_location("usb_check", board_usb.CHECKER)
usb_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usb_check)


class BoardUsbTests(unittest.TestCase):
    def test_evidence_binds_sources_report_binary_and_transferred_pixels(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(board_usb, "ROOT", Path(temporary)), \
             mock.patch.object(board_usb, "sources", return_value={"source": "hash"}):
            directory = Path(temporary) / "artifacts/hardware/run"
            directory.mkdir(parents=True)
            payloads = {"cases.json": b"[]", "renderer.stdout": b"binary", "transfer.bin": b"transfer",
                        "download.stdout": b"transfer", "board-frame.ppm": b"pixels", "host-frame.ppm": b"pixels"}
            native = dict(status="PASS", renderer_sha256=hashlib.sha256(b"binary").hexdigest(),
                          cases_sha256=hashlib.sha256(b"[]").hexdigest())
            payloads["report.stdout"] = json.dumps(native).encode()
            for name, content in payloads.items():
                (directory / name).write_bytes(content)
            record = dict(status="PASS", stage="H0-usb-userspace", directory=str(directory.relative_to(board_usb.ROOT)),
                          source_sha256=board_usb.sources(), native=native,
                          artifact_sha256={name: hashlib.sha256(content).hexdigest() for name, content in payloads.items()},
                          render_sha256=hashlib.sha256(b"pixels").hexdigest(),
                          usb_round_trip=dict(sha256=hashlib.sha256(b"transfer").hexdigest()))
            self.assertTrue(board_usb.evidence_current(record))
            for replacement in ({"status": "FAIL"}, {"source_sha256": {}}, {"directory": "/etc"},
                                {"artifact_sha256": {}}, {"native": {"status": "PASS"}}):
                with self.subTest(replacement=replacement):
                    self.assertFalse(board_usb.evidence_current({**record, **replacement}))
            (directory / "renderer.stdout").write_bytes(b"changed")
            self.assertFalse(board_usb.evidence_current(record))

    def test_route_must_be_direct_and_match_explicit_interface(self):
        route = dict(dev="usb0", prefsrc="192.168.3.100")
        self.assertEqual(board_usb.validate_route([route], "usb0"), "192.168.3.100")
        for invalid in ([], [route, route], [{**route, "dev": "wlan0"}],
                        [{**route, "gateway": "192.168.3.254"}]):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                board_usb.validate_route(invalid, "usb0")

    def test_usb_identity_requires_pynq_not_arbitrary_network_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device = root / "usb-device"
            interface = root / "net/usb0"
            device.mkdir()
            interface.mkdir(parents=True)
            (interface / "device").symlink_to(device, target_is_directory=True)
            (device / "idVendor").write_text("03fd\n")
            (device / "idProduct").write_text("010c\n")
            self.assertEqual(board_usb.validate_usb("usb0", root / "net"), "03fd:010c")
            (device / "idVendor").write_text("ffff\n")
            with self.assertRaises(RuntimeError):
                board_usb.validate_usb("usb0", root / "net")
            with self.assertRaises(ValueError):
                board_usb.validate_usb("../escape", root / "net")

    def test_scratch_storage_verifies_and_removes_only_own_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "keep"
            sentinel.write_text("untouched")
            result = usb_check.storage_check(root, 2**20)
            self.assertEqual(result["sha256"], hashlib.sha256(bytes(range(256)) * 4096).hexdigest())
            self.assertEqual(list(root.iterdir()), [sentinel])
            for size in (0, 2**20 + 1, 17 * 2**20):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    usb_check.storage_check(root, size)

    def test_memory_workload_is_bounded_and_affinity_is_reported(self):
        original = os.sched_getaffinity(0)
        try:
            cpu = min(original)
            result = usb_check.memory_worker(cpu, 2**20, 2)
            self.assertEqual(result["affinity"], [cpu])
            self.assertEqual(result["bytes"], 2**20)
            self.assertEqual(len(result["sha256"]), 2)
            self.assertNotEqual(*result["sha256"])
            with self.assertRaises(ValueError):
                usb_check.memory_worker(cpu, 33 * 2**20)
        finally:
            os.sched_setaffinity(0, original)

    def test_reference_requires_exact_pixels_length_and_valid_geometry(self):
        case = dict(width=1, height=1, mode=0, threshold=0, seed=0, sha256=hashlib.sha256(b"x").hexdigest())
        with mock.patch.object(usb_check.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"x", b"")):
            self.assertEqual(len(usb_check.reference_check(Path("/renderer"), [case])), 1)
        for data in (b"", b"y", b"xx"):
            with self.subTest(data=data), \
                 mock.patch.object(usb_check.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, data, b"")), \
                 self.assertRaises(RuntimeError):
                usb_check.reference_check(Path("/renderer"), [case])
        for replacement in ({"width": 0}, {"height": 1025}, {"mode": 3}, {"seed": -1}, {"threshold": True}):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                usb_check.reference_check(Path("/renderer"), [{**case, **replacement}])

    def test_reference_cases_cover_boundaries_without_invalid_sobel(self):
        cases = board_usb.reference_cases()
        self.assertEqual(len(cases), 78)
        self.assertEqual({case["mode"] for case in cases}, set(range(4)))
        self.assertEqual({case["threshold"] for case in cases}, {0, 127, 255})
        self.assertTrue(any(case["width"] == case["height"] == 1024 for case in cases))
        self.assertTrue(all(len(case["sha256"]) == 64 for case in cases))
        self.assertTrue(all(min(case["width"], case["height"]) >= 3 for case in cases if case["mode"] == 3))


if __name__ == "__main__":
    unittest.main()
