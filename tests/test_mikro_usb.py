import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import mikro_usb


def candidate():
    return dict(kernel="5.15.19-xilinx-v2022.1", recipe_sha256="a" * 64,
                renderer_sha256="b" * 64, ssh=dict(public_key_sha256="c" * 64))


def snapshot():
    return {"/proc/device-tree/compatible": "avnet,ultra96-rev1\0xlnx,zynqmp\0",
            "/proc/sys/kernel/osrelease": candidate()["kernel"] + "\n",
            "/proc/sys/kernel/random/boot_id": "12345678-1234-1234-1234-123456789abc\n",
            "/proc/cmdline": "root=/dev/mmcblk0p2 rw rootfstype=ext4 rootwait\n",
            "/proc/mounts": "/dev/root / ext4 rw,relatime 0 0\nproc /proc proc rw 0 0\n",
            "/etc/axi-lab-release": "AXI_LAB_MIKRO_RECIPE_SHA256=" + "a" * 64 + "\n",
            "renderer.sha256": "b" * 64 + "  /usr/bin/axi-lab-display\n",
            "key.sha256": "c" * 64 + "  /root/.ssh/authorized_keys\n",
            "private-modes": "0:0:600\n0:0:600\n", "dmesg": "Linux booted\n"}


class MikroUsbTests(unittest.TestCase):
    def test_sd_boot_requires_exact_identity_recipe_kernel_and_payload(self):
        self.assertEqual(mikro_usb.validate_snapshot(snapshot(), candidate()),
                         "12345678-1234-1234-1234-123456789abc")
        for field, value in (("/proc/device-tree/compatible", "avnet,ultra96-v2\0"),
                             ("/proc/sys/kernel/osrelease", "6.12.109"),
                             ("/proc/sys/kernel/random/boot_id", "unknown"),
                             ("/etc/axi-lab-release", "wrong"), ("renderer.sha256", "bad"),
                             ("key.sha256", "bad"), ("private-modes", "0:0:644\n0:0:600\n")):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                mikro_usb.validate_snapshot({**snapshot(), field: value}, candidate())

    def test_initramfs_wrong_root_readonly_and_faulted_boots_are_not_accepted(self):
        original = snapshot()
        for command in ("root=/dev/ram0 rw rootfstype=ext4", "root=/dev/mmcblk0p2 rootfstype=ext2",
                        original["/proc/cmdline"].strip() + " axi_lab.mikro_smoke=1",
                        original["/proc/cmdline"].strip() + " root=/dev/mmcblk0p2"):
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                mikro_usb.validate_snapshot({**original, "/proc/cmdline": command}, candidate())
        for mounts in ("rootfs / rootfs rw 0 0\n", "/dev/root / ext4 ro 0 0\n",
                       "/dev/root / ext4 rw 0 0\n/dev/root / ext4 rw 0 0\n", "malformed"):
            with self.subTest(mounts=mounts), self.assertRaises(RuntimeError):
                mikro_usb.validate_snapshot({**original, "/proc/mounts": mounts}, candidate())
        for fault in ("rcu: detected stall", "SError", "Kernel panic", "Oops:", "BUG:", "DMA-API:"):
            with self.subTest(fault=fault), self.assertRaises(RuntimeError):
                mikro_usb.validate_snapshot({**original, "dmesg": fault}, candidate())

    def test_services_require_standalone_completion_marker(self):
        marker = "AXI_LAB_MIKRO_SERVICES_PASS wifi=CONFIG_ONLY modules=HASH_ONLY tls=BUILD_ONLY"
        mikro_usb.validate_services(marker + "\n")
        with self.assertRaises(RuntimeError):
            mikro_usb.validate_services(marker + "\n", {"framefx_pl_included": True})
        mikro_usb.validate_services(marker + "\nAXI_LAB_MIKRO_PL_BUNDLE_PASS programming=NOT_ATTEMPTED\n",
                                    {"framefx_pl_included": True})
        for output in ("", "echo " + marker, marker + " FAILED"):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                mikro_usb.validate_services(output)

    def test_frozen_candidate_rejects_tampering_but_does_not_require_current_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            image = artifacts / "image"
            ramdisk = artifacts / "ramdisk"
            with image.open("wb") as output:
                output.truncate(271581184)
            ramdisk.write_bytes(b"ramdisk")
            sources = {"historical-source": "digest"}
            record = {**candidate(), "status": "PASS", "stage": "H0-mikro-candidate", "source_sha256": sources,
                      "recipe_sha256": mikro_usb.mikro.recipe_id(sources), "image": "artifacts/image",
                      "ramdisk": "artifacts/ramdisk", "image_bytes": 271581184,
                      "image_sha256": mikro_usb.lab.sha256(image), "ramdisk_sha256": mikro_usb.lab.sha256(ramdisk),
                      "reference_ppm_sha256": "d" * 64}
            with mock.patch.object(mikro_usb.lab, "ROOT", root), mock.patch.object(mikro_usb.lab, "ARTIFACTS", artifacts):
                mikro_usb.validate_candidate(record)
                for replacement in ({"status": "FAIL"}, {"kernel": "6.12"}, {"recipe_sha256": "e" * 64},
                                    {"image_bytes": 512}, {"image_sha256": "f" * 64}, {"ramdisk": "../escape"}):
                    with self.subTest(replacement=replacement), self.assertRaises((ValueError, RuntimeError, FileNotFoundError)):
                        mikro_usb.validate_candidate({**record, **replacement})

    def test_transport_pins_host_key_and_direct_usb_route_without_password_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key, known = root / "key", root / "known"
            key.write_text("private fixture")
            known.write_text("host fixture")
            key.chmod(0o600)
            known.chmod(0o600)
            route = json.dumps([dict(dev="usb0", prefsrc="192.168.3.2")])
            with mock.patch.object(mikro_usb.board_usb, "validate_usb", return_value="03fd:010d"), \
                 mock.patch.object(mikro_usb.subprocess, "check_output", return_value=route), \
                 mock.patch.object(mikro_usb.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"ok", b"")) as run:
                connection = mikro_usb.Connection("usb0", "192.168.3.1", key, known, root)
                self.assertIn("StrictHostKeyChecking=yes", connection.command)
                self.assertIn("BatchMode=yes", connection.command)
                self.assertIn("IdentitiesOnly=yes", connection.command)
                self.assertIn("root@192.168.3.1", connection.command)
                self.assertEqual(connection.run(["cat", "/etc/axi-lab-release"], "release"), b"ok")
                self.assertEqual(run.call_count, 1)
                with mock.patch.object(mikro_usb.subprocess, "check_output", return_value=json.dumps([
                        dict(dev="wlp1s0", prefsrc="192.168.4.2", gateway="192.168.4.1")])):
                    with self.assertRaises(RuntimeError):
                        connection.run(["cat", "/etc/axi-lab-release"], "bad-route")
                self.assertEqual(run.call_count, 1)
                with mock.patch.object(mikro_usb.board_usb, "validate_usb", return_value="03fd:010c"):
                    with self.assertRaises(RuntimeError):
                        connection.run(["cat", "/etc/axi-lab-release"], "changed-device")
                key.chmod(0o644)
                with self.assertRaises(ValueError):
                    mikro_usb.Connection("usb0", "192.168.3.1", key, known, root)

    def test_invalid_evidence_is_never_current(self):
        for record in ({}, dict(directory="../outside"), dict(directory="artifacts/no-evidence")):
            self.assertFalse(mikro_usb.evidence_current(record))

    def test_full_saved_evidence_rechecks_pixels_transfer_and_boot_not_just_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "artifacts/hardware/check"
            directory.mkdir(parents=True)
            frozen = {**candidate(), "image_sha256": "d" * 64,
                      "reference_ppm_sha256": hashlib.sha256(b"frame").hexdigest()}
            cases = [dict(width=1, height=1, mode=0, threshold=127, seed=1,
                          sha256=hashlib.sha256(b"x").hexdigest())] * 78
            connection = mock.Mock()
            payload = {}

            def remote(arguments, name, data=None, **kwargs):
                output = b""
                if name == "services":
                    output = b"AXI_LAB_MIKRO_SERVICES_PASS wifi=CONFIG_ONLY modules=HASH_ONLY tls=BUILD_ONLY\n"
                elif name == "workspace":
                    output = b"/tmp/axi-mikro-check.fixture\n"
                elif name == "upload":
                    payload["transfer"] = data
                elif name == "download":
                    output = payload["transfer"]
                elif name.startswith("pixels-"):
                    output = b"x"
                elif name == "frame":
                    output = b"frame"
                (directory / (name + ".stdout")).write_bytes(output)
                return output

            def capture(prefix):
                state = snapshot()
                mikro_usb.lab.write_json(directory / (prefix + ".json"), state)
                return state

            connection.run.side_effect = remote
            connection.snapshot.side_effect = capture
            with mock.patch.object(mikro_usb.board_usb, "reference_cases", return_value=cases), \
                 mock.patch.object(mikro_usb.lab, "ROOT", root), \
                 mock.patch.object(mikro_usb.lab, "ARTIFACTS", root / "artifacts"), \
                 mock.patch.object(mikro_usb, "validate_candidate"), \
                 mock.patch.object(mikro_usb, "sources", return_value={"input": "hash"}):
                boot = mikro_usb.verify(connection, frozen, directory)
                mikro_usb.lab.write_json(directory / "candidate.json", frozen)

                def evidence():
                    return dict(status="PASS", stage="H0-mikro-usb", source_sha256={"input": "hash"},
                                directory="artifacts/hardware/check", boot=boot, image_sha256=frozen["image_sha256"],
                                artifact_sha256={path.name: mikro_usb.lab.sha256(path) for path in directory.iterdir()})

                record = evidence()
                self.assertTrue(mikro_usb.evidence_current(record))
                for name in ("pixels-0.stdout", "download.stdout", "frame.stdout", "services.stdout"):
                    path = directory / name
                    original = path.read_bytes()
                    path.write_bytes(b"wrong")
                    self.assertFalse(mikro_usb.evidence_current(record), name)
                    self.assertFalse(mikro_usb.evidence_current(evidence()), name)
                    path.write_bytes(original)
                changed = {**snapshot(), "/proc/sys/kernel/random/boot_id": "12345678-1234-1234-1234-123456789abd\n"}
                mikro_usb.lab.write_json(directory / "after.json", changed)
                self.assertFalse(mikro_usb.evidence_current(evidence()))


if __name__ == "__main__":
    unittest.main()
