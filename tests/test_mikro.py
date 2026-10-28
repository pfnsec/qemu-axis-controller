import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import framefx
import mikro


def mbr(lock):
    result = bytearray(512)
    result[510:] = b"\x55\xaa"
    for index, partition in enumerate(lock["partitions"]):
        offset = 446 + 16 * index
        result[offset + 4] = int(partition["type"], 16)
        struct.pack_into("<II", result, offset + 8, partition["start"], partition["size"])
    return bytes(result)


class MikroImageTests(unittest.TestCase):
    def test_mikro_build_identity_is_consistent(self):
        self.assertEqual(mikro.PAYLOAD.name, "ultra96-mikro")
        self.assertEqual(mikro.BUILD.name, "ultra96-mikro")
        sources = mikro.inputs()
        self.assertIn("scripts/mikro.py", sources)
        self.assertIn("linux/buildroot/configs/ultra96_mikro_defconfig", sources)
        self.assertIn("linux/buildroot/package/axi-mikro-display/axi-mikro-display.mk", sources)
        configuration = (ROOT / "linux/buildroot/configs/ultra96_mikro_defconfig").read_text()
        self.assertIn("BR2_PACKAGE_AXI_MIKRO_DISPLAY=y", configuration)
        startup = (mikro.PAYLOAD / "overlay/etc/init.d/S99mikrosmoke").read_text()
        self.assertIn("axi_lab.mikro_smoke=1", startup)
        self.assertIn("AXI_LAB_MIKRO_USERSPACE_PASS", startup)

    def test_only_root_partition_length_changes(self):
        lock = json.loads(mikro.board.LOCK_FILE.read_text())
        original = mbr(lock)
        modified = mikro.resized_mbr(original, lock, mikro.ROOTFS_BYTES)
        self.assertEqual(original[:474], modified[:474])
        self.assertEqual(original[478:], modified[478:])
        self.assertEqual(struct.unpack_from("<I", modified, 474)[0], 262144)
        self.assertEqual(lock["partitions"][1]["start"] * 512 + mikro.ROOTFS_BYTES, 271581184)

    def test_bad_geometry_and_extra_partitions_rejected(self):
        lock = json.loads(mikro.board.LOCK_FILE.read_text())
        for size in (0, -512, 513, 2**32 * 512):
            with self.subTest(size=size), self.assertRaises(ValueError):
                mikro.resized_mbr(mbr(lock), lock, size)
        for offset in (510, 450, 454, 474, 478):
            damaged = bytearray(mbr(lock))
            damaged[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                mikro.resized_mbr(bytes(damaged), lock, mikro.ROOTFS_BYTES)

    def test_image_assembly_preserves_boot_bytes_and_exact_rootfs(self):
        lock = {"partitions": [{"start": 8, "size": 4, "type": "c"}, {"start": 16, "size": 64, "type": "83"}]}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(mikro, "ROOTFS_BYTES", 2048):
            directory = Path(temporary)
            base, rootfs, output = (directory / name for name in ("base.img", "rootfs.ext4", "mikro.img"))
            base.write_bytes(mbr(lock) + bytes(range(256)) * 200)
            rootfs.write_bytes(b"root" * 512)
            record = mikro.assemble(base, rootfs, output, lock)
            result = output.read_bytes()
            self.assertEqual(len(result), 16 * 512 + 2048)
            self.assertEqual(result[512:16 * 512], base.read_bytes()[512:16 * 512])
            self.assertEqual(result[16 * 512:], rootfs.read_bytes())
            self.assertEqual(hashlib.sha256(result[512:16 * 512]).hexdigest(), record["sha256"])
            with self.assertRaises(FileExistsError):
                mikro.assemble(base, rootfs, output, lock)

    def test_truncated_sources_rejected(self):
        with self.assertRaises(RuntimeError):
            mikro.copy_exact(io.BytesIO(b"short"), io.BytesIO(), 6)
        with self.assertRaises(ValueError):
            mikro.copy_exact(io.BytesIO(b"short"), io.BytesIO(), -1)

    def test_startup_scripts_executable_and_ssh_key_only(self):
        for directory in ("overlay/etc/init.d", "overlay/usr/sbin"):
            for script in (mikro.PAYLOAD / directory).iterdir():
                self.assertTrue(script.stat().st_mode & 0o111, str(script))
                subprocess.run(["sh", "-n", script], check=True, timeout=5)
        self.assertIn('DROPBEAR_ARGS="-s -g"', (mikro.PAYLOAD / "overlay/etc/default/dropbear").read_text())
        gadget = (mikro.PAYLOAD / "overlay/etc/init.d/S30usbgadget").read_text()
        self.assertNotIn("functions/mass_storage", gadget)
        self.assertIn("avnet,ultra96-rev1", gadget)
        self.assertNotIn("modprobe", (mikro.PAYLOAD / "overlay/etc/init.d/S10mdev").read_text())
        self.assertNotIn("modprobe", (mikro.PAYLOAD / "overlay/etc/mdev.conf").read_text())

    def test_recipe_identity_is_order_independent_and_content_sensitive(self):
        self.assertEqual(mikro.recipe_id({"first": "1", "second": "2"}),
                         mikro.recipe_id({"second": "2", "first": "1"}))
        self.assertNotEqual(mikro.recipe_id({"first": "1"}), mikro.recipe_id({"first": "2"}))
        self.assertNotEqual(mikro.recipe_id({"first": "1"}), mikro.recipe_id({"first": "1"}, {"native": "hash"}))
        self.assertNotEqual(mikro.recipe_id({"first": "1"}, {"native": "hash"}),
                            mikro.recipe_id({"first": "1"}, {"native": "changed"}))

    def test_mdev_preserves_root_only_framefx_device_permissions(self):
        rules = (mikro.PAYLOAD / "overlay/etc/mdev.conf").read_text().splitlines()
        matches = [line.split() for line in rules if line.strip() and not line.startswith("#")
                   and re.fullmatch(line.split()[0], "framefx0")]
        self.assertTrue(matches, "mdev's default mode must not replace the driver's 0600")
        self.assertEqual(matches[0], ["framefx0", "root:root", "600"])

    def test_pl_profile_requires_current_native_and_bitstream_inputs(self):
        self.assertIsNone(mikro.pl_inputs(None))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            native = artifacts / "native.json"
            native.write_text("{}\n")
            (artifacts / "ultra96-pl-bitstream.json").write_text("{}\n")
            with mock.patch.object(mikro.lab, "ROOT", root), mock.patch.object(mikro.lab, "ARTIFACTS", artifacts), \
                 mock.patch.object(mikro.pl_bundle, "validate_native") as validate, \
                 mock.patch.object(mikro.pl_bundle.pl, "evidence_current", return_value=True):
                original = mikro.pl_inputs(native)
                validate.assert_called_once_with({}, native.parent)
                self.assertEqual(original["native_record"], "artifacts/native.json")
                native.write_text('{"changed": true}\n')
                self.assertNotEqual(original, mikro.pl_inputs(native))
                with mock.patch.object(mikro.pl_bundle.pl, "evidence_current", return_value=False):
                    with self.assertRaisesRegex(ValueError, "bitstream"):
                        mikro.pl_inputs(native)
                with mock.patch.object(mikro.pl_bundle, "validate_native", side_effect=ValueError("stale native")):
                    with self.assertRaisesRegex(ValueError, "stale native"):
                        mikro.pl_inputs(native)
                link = artifacts / "link.json"
                link.symlink_to(native)
                with self.assertRaises(ValueError):
                    mikro.pl_inputs(link)

    def test_pl_binaries_require_aarch64_and_exact_vendor_kernel(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            headers = {}
            for name in ("framefx.ko", "framefx-probe", "framefx-test"):
                header = bytearray(20)
                header[:6] = b"\x7fELF\x02\x01"
                struct.pack_into("<HH", header, 16, 1 if name.endswith(".ko") else 3, 183)
                headers[name] = bytes(header)
                (directory / name).write_bytes(header)
            with mock.patch.object(mikro.subprocess, "check_output", return_value="5.15.19-xilinx-v2022.1 SMP\n"):
                mikro.validate_pl_binaries(directory)
                for name in headers:
                    (directory / name).write_bytes(b"not an ARM ELF")
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        mikro.validate_pl_binaries(directory)
                    (directory / name).write_bytes(headers[name])
            with mock.patch.object(mikro.subprocess, "check_output", return_value="6.12.109 SMP\n"):
                with self.assertRaisesRegex(ValueError, "vendor kernel"):
                    mikro.validate_pl_binaries(directory)

    def test_pl_smoke_requires_bundle_validation_without_programming(self):
        candidate = dict(recipe_sha256="recipe", renderer_sha256="binary", reference_ppm_sha256="pixels", framefx_pl_included=True)
        lines = ["AXI_LAB_MIKRO_USERSPACE_PASS hardware=NOT_TESTED", "AXI_LAB_MIKRO_RECIPE_SHA256=recipe",
                 "AXI_LAB_INIT_BEGIN", "AXI_LAB_INIT_RESULT service=S30usbgadget status=0",
                 "AXI_LAB_MIKRO_SERVICES_PASS wifi=CONFIG_ONLY modules=HASH_ONLY tls=BUILD_ONLY",
                 "pixels  /tmp/mikro.ppm", "binary  /usr/bin/axi-lab-display", "[    9.081] reboot: Power down"]
        with self.assertRaises(RuntimeError):
            mikro.validate_smoke("\n".join(lines), candidate)
        lines.append("AXI_LAB_MIKRO_PL_BUNDLE_PASS programming=NOT_ATTEMPTED")
        mikro.validate_smoke("\n".join(lines), candidate)

    def test_image_evidence_rejects_stale_and_modified_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = directory / "image"
            ramdisk = directory / "ramdisk"
            image.write_bytes(b"image")
            ramdisk.write_bytes(b"ramdisk")
            sources = {"input": "hash"}
            record = dict(status="PASS", stage="H0-mikro-candidate", image="image", ramdisk="ramdisk",
                          image_bytes=5, image_sha256=mikro.lab.sha256(image), ramdisk_sha256=mikro.lab.sha256(ramdisk),
                          source_sha256=sources, recipe_sha256=mikro.recipe_id(sources))
            with mock.patch.object(mikro.lab, "ROOT", directory), mock.patch.object(mikro, "inputs", return_value=sources):
                self.assertTrue(mikro.evidence_current(record))
                for field, value in (("status", "FAIL"), ("stage", "H0-image"), ("stage", "H0-lean-candidate"), ("image_bytes", 4),
                                     ("image_sha256", "bad"), ("ramdisk_sha256", "bad"), ("recipe_sha256", "bad"),
                                     ("source_sha256", {}), ("image", "../escape"), ("framefx_pl_included", True),
                                     ("pl_inputs", True)):
                    with self.subTest(field=field):
                        self.assertFalse(mikro.evidence_current({**record, field: value}))
                self.assertFalse(mikro.evidence_current({**record, "wifi_config": True}))
                with mock.patch.object(mikro, "private_config", return_value={"path": "private", "sha256": "changed"}):
                    self.assertFalse(mikro.evidence_current({**record, "wifi_config": {"path": "private", "sha256": "original"}}))
                ramdisk.write_bytes(b"changed")
                self.assertFalse(mikro.evidence_current(record))
                self.assertFalse(mikro.evidence_current({}))

    def test_smoke_requires_exact_markers_and_matching_pixels(self):
        candidate = dict(recipe_sha256="recipe", reference_ppm_sha256="pixels", renderer_sha256="binary")
        lines = ["AXI_LAB_MIKRO_USERSPACE_PASS hardware=NOT_TESTED", "AXI_LAB_MIKRO_RECIPE_SHA256=recipe",
                 "AXI_LAB_INIT_BEGIN", "AXI_LAB_INIT_RESULT service=S30usbgadget status=0",
                 "AXI_LAB_MIKRO_SERVICES_PASS wifi=CONFIG_ONLY modules=HASH_ONLY tls=BUILD_ONLY",
                 "pixels  /tmp/mikro.ppm", "binary  /usr/bin/axi-lab-display", "[    9.081] reboot: Power down"]
        mikro.validate_smoke("\n".join(lines), candidate)
        for index in range(len(lines)):
            damaged = lines.copy()
            damaged[index] = "echo " + damaged[index]
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                mikro.validate_smoke("\n".join(damaged), candidate)
        for failure in ("AXI_LAB_MIKRO_USERSPACE_FAIL", "Kernel panic", "modprobe: missing modules",
                        "AXI_LAB_INIT_LOG_FAIL", "AXI_LAB_USB_FAIL stage=network status=1",
                        "AXI_LAB_INIT_RESULT service=S10mdev status=7"):
            with self.subTest(failure=failure), self.assertRaises(RuntimeError):
                mikro.validate_smoke("\n".join([*lines, failure]), candidate)

    def test_smoke_evidence_binds_image_and_executed_binaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / "binary"
            binary.write_bytes(b"qemu")
            sources = {"input": "hash"}
            record = dict(status="PASS", stage="mikro-userspace-smoke", source_sha256=sources,
                          image_sha256="image", binary_sha256={"binary": mikro.lab.sha256(binary)})
            candidate = {"image_sha256": "image"}
            with mock.patch.object(mikro.lab, "ROOT", directory), mock.patch.object(mikro, "inputs", return_value=sources), \
                    mock.patch.object(mikro, "evidence_current", return_value=True):
                self.assertTrue(mikro.smoke_current(record, candidate))
                self.assertFalse(mikro.smoke_current(record, {"image_sha256": "different"}))
                self.assertFalse(mikro.smoke_current({**record, "binary_sha256": {}}, candidate))
                binary.write_bytes(b"changed")
                self.assertFalse(mikro.smoke_current(record, candidate))

    def test_module_dependency_closure_preserves_order_and_deduplicates(self):
        dependencies = "kernel/core.ko:\nkernel/radio.ko: kernel/core.ko\nkernel/sdio.ko:\nextra/unused.ko:\n"
        self.assertEqual(mikro.module_closure(dependencies, ["kernel/radio.ko", "kernel/sdio.ko", "kernel/core.ko"]),
                         ["kernel/core.ko", "kernel/radio.ko", "kernel/sdio.ko"])

    def test_module_dependency_cycles_missing_paths_and_escapes_fail(self):
        for dependencies in ("kernel/core.ko: kernel/missing.ko", "kernel/core.ko: kernel/core.ko",
                             "kernel/core.ko: /escape.ko", "kernel/core.ko: kernel/../escape.ko",
                             "kernel/core.ko:\nkernel/core.ko:", "malformed"):
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                mikro.module_closure(dependencies, ["kernel/core.ko"])

    def test_wifi_configuration_must_be_private_and_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            config = artifacts / "wifi.conf"
            config.write_text('ctrl_interface=/run/wpa_supplicant\n')
            with mock.patch.object(mikro.lab, "ROOT", root), mock.patch.object(mikro.lab, "ARTIFACTS", artifacts):
                config.chmod(0o600)
                original = mikro.private_config(config)
                self.assertEqual(original["path"], "artifacts/wifi.conf")
                self.assertIsNone(mikro.private_config(None))
                config.write_text("changed\n")
                self.assertNotEqual(original, mikro.private_config(config))
                config.chmod(0o644)
                with self.assertRaises(RuntimeError):
                    mikro.private_config(config)
                config.chmod(0o600)
                link = artifacts / "link"
                link.symlink_to(config)
                with self.assertRaises(RuntimeError):
                    mikro.private_config(link)
                outside = root / "tracked.conf"
                outside.write_text("secret")
                outside.chmod(0o600)
                with self.assertRaises(RuntimeError):
                    mikro.private_config(outside)


class MikroNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "sys/class/net/wlan0/wireless").mkdir(parents=True)
        (self.root / "run").mkdir()
        binaries = self.root / "bin"
        binaries.mkdir()
        self.log = self.root / "commands.log"
        self.environment = {**os.environ, "PATH": f"{binaries}:/usr/bin:/bin", "COMMAND_LOG": str(self.log)}
        for command in ("ip", "ifconfig", "logger", "udhcpc", "start-stop-daemon"):
            target = binaries / command
            target.write_text('#!/bin/sh\nprintf "%s" "${0##*/}" >> "$COMMAND_LOG"\n'
                              'printf " <%s>" "$@" >> "$COMMAND_LOG"\nprintf "\\n" >> "$COMMAND_LOG"\n')
            target.chmod(0o755)
        for name in ("axi-lab-wifi-dhcp", "axi-lab-wifi-event"):
            content = (mikro.PAYLOAD / "overlay/usr/sbin" / name).read_text()
            content = content.replace("/sys/class/net/", str(self.root / "sys/class/net") + "/")
            content = content.replace("/run/", str(self.root / "run") + "/")
            content = content.replace("/usr/sbin/axi-lab-wifi-dhcp", str(self.root / "axi-lab-wifi-dhcp"))
            (self.root / name).write_text(content)
            (self.root / name).chmod(0o755)

    def run_dhcp(self, action, **variables):
        return subprocess.run([self.root / "axi-lab-wifi-dhcp", action], timeout=5, capture_output=True,
                              env={**self.environment, "interface": "wlan0", **variables})

    def test_dhcp_configures_only_wireless_and_replaces_dns(self):
        result = self.run_dhcp("bound", ip="192.0.2.5", subnet="255.255.255.0", router="192.0.2.1", dns="192.0.2.53 192.0.2.54")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "run/resolv.conf").read_text(), "nameserver 192.0.2.53\nnameserver 192.0.2.54\n")
        self.assertIn("<metric> <600>", self.log.read_text())
        self.assertNotIn("usb0", self.log.read_text())
        self.assertEqual(self.run_dhcp("renew", ip="192.0.2.6", subnet="255.255.255.0", dns="192.0.2.55").returncode, 0)
        self.assertEqual((self.root / "run/resolv.conf").read_text(), "nameserver 192.0.2.55\n")
        self.assertEqual(self.run_dhcp("deconfig").returncode, 0)
        self.assertEqual((self.root / "run/resolv.conf").read_text(), "")

    def test_dhcp_rejects_nonwireless_interfaces_and_untrusted_addresses(self):
        for variables in ({"interface": "usb0"}, {"interface": "../../escape"}, {"ip": "999.1.2.3"},
                          {"dns": "1.2.3.4\nsearch attacker.invalid"}, {"router": "$(touch /tmp/unsafe)"},
                          {"subnet": ""}, {"subnet": "255.0.255.0"}, {"router": "198.51.100.1"}, {"ip": "--help"}):
            result = self.run_dhcp("bound", **{"ip": "192.0.2.5", "subnet": "255.255.255.0", **variables})
            with self.subTest(variables=variables):
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())

    def test_overlapping_wifi_subnets_cannot_break_usb_rescue(self):
        for address, mask in (("192.168.3.25", "255.255.255.0"), ("192.168.5.25", "255.255.0.0"),
                              ("192.168.2.25", "255.255.254.0"), ("192.168.3.1", "255.255.255.255")):
            with self.subTest(address=address, mask=mask):
                result = self.run_dhcp("bound", ip=address, subnet=mask)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("ifconfig", self.log.read_text())
                self.assertNotIn("ip <", self.log.read_text())
        self.assertEqual(self.run_dhcp("bound", ip="192.168.4.25", subnet="255.255.255.0").returncode, 0)

    def test_connected_events_start_bounded_background_dhcp_without_duplicates(self):
        command = [self.root / "axi-lab-wifi-event", "wlan0", "CONNECTED"]
        subprocess.run(command, env=self.environment, check=True, timeout=5)
        self.assertIn("udhcpc <-b> <-R> <-i> <wlan0>", self.log.read_text())
        (self.root / "run/udhcpc-axi-wlan0.pid").write_text(str(os.getpid()))
        before = self.log.read_text()
        subprocess.run(command, env=self.environment, check=True, timeout=5)
        self.assertEqual(self.log.read_text(), before)
        subprocess.run([self.root / "axi-lab-wifi-event", "wlan0", "DISCONNECTED"],
                       env=self.environment, check=True, timeout=5)
        self.assertFalse((self.root / "run/udhcpc-axi-wlan0.pid").exists())


class MikroDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        cls.binary = cls.directory / "display"
        subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        mikro.PAYLOAD / "display.c", "-o", cls.binary], check=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_native_pixels_match_independent_reference(self):
        for width, height in ((1, 1), (3, 3), (65, 9), (256, 192)):
            for mode in range(4):
                if mode == 3 and min(width, height) < 3:
                    continue
                for threshold in (0, 127, 255):
                    seed = 0xffffffff
                    pixels = bytes(((row * 0x45d9f3b + column * 0x119de1f3 + seed * 17)
                                    ^ (column >> 3) ^ (row << 5)) & 255
                                   for row in range(height) for column in range(width))
                    actual = subprocess.check_output([self.binary, "--reference", str(width), str(height),
                                                      str(mode), str(threshold), str(seed)], timeout=5)
                    with self.subTest(width=width, height=height, mode=mode, threshold=threshold):
                        self.assertEqual(actual, framefx.transform(pixels, width, height, mode, threshold))

    def test_invalid_cli_does_not_open_framebuffer(self):
        for arguments in ([], ["--unknown"], ["--reference", "0", "3", "0", "0", "1"],
                          ["--reference", "1", "1", "3", "127", "1"],
                          ["--reference", "-3", "3", "0", "0", "1"],
                          ["--reference", "3", "3", "4", "0", "1"],
                          ["--reference", "3", "3", "0", "256", "1"],
                          ["--reference", "3", "3", "0", "0", "4294967296"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([self.binary, *arguments], capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 2)

    def test_headless_scenes_are_bounded_and_animate(self):
        hashes = []
        for mode, frame in ((0, 0), (1, 0), (2, 0), (3, 0), (3, 9)):
            output = self.directory / "scene.ppm"
            subprocess.run([self.binary, "--render", output, str(mode), str(frame)], check=True, timeout=5)
            content = output.read_bytes()
            self.assertTrue(content.startswith(b"P6\n800 600\n255\n"))
            self.assertEqual(len(content), len(b"P6\n800 600\n255\n") + 800 * 600 * 3)
            hashes.append(hashlib.sha256(content).hexdigest())
        self.assertEqual(len(set(hashes)), 5)

    def test_geometry_stride_and_color_packing(self):
        binary = self.directory / "unit"
        subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        ROOT / "tests/mikro_display_unit.c", "-o", binary], check=True, timeout=30)
        result = subprocess.check_output([binary], text=True, timeout=5)
        self.assertIn("MIKRO_DISPLAY_UNIT_PASS", result)


if __name__ == "__main__":
    unittest.main()
