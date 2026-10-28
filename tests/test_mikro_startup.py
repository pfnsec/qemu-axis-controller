import fcntl
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "linux/hardware/ultra96-mikro/overlay"
SHELL = shlex.split(os.environ.get("MIKRO_TEST_SHELL", "/bin/sh"))

COMMAND = r'''#!/usr/bin/python3
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["FIXTURE_ROOT"])
name = Path(sys.argv[0]).name
arguments = sys.argv[1:]
binding = root / "sys/kernel/config/usb_gadget/axi_lab/UDC"
with (root / "commands.log").open("a") as output:
    output.write(name + " " + " ".join(arguments) + "\n")
if os.environ.get("FAIL_COMMAND") == name:
    sys.exit(1)
if name == "mkdir":
    subprocess.run(["/bin/mkdir", *arguments], check=True)
    if binding.parent.exists() and not binding.exists():
        binding.touch(mode=0o444 if os.environ.get("BIND_FAIL") else 0o600)
elif name == "ln":
    try:
        target = Path(arguments[1]).resolve(strict=True)
    except FileNotFoundError:
        sys.exit("configfs target must resolve from the caller's working directory")
    if not target.is_dir() or not target.is_relative_to(root / "sys/kernel/config/usb_gadget"):
        sys.exit("configfs target must be a directory in the same filesystem")
    destination = Path(arguments[2]) / target.name
    destination.symlink_to(os.path.relpath(target, destination.parent))
elif name == "sleep":
    counter = root / "sleeps"
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if str(count) == os.environ.get("CONTROLLER_DELAY"):
        controller = root / "sys/class/udc/test.dwc3"
        controller.mkdir()
        (controller / "state").write_text("not attached\n")
    if os.environ.get("BIND_RECOVER") and binding.exists():
        binding.chmod(0o600)
elif name == "start-stop-daemon":
    active = root / "dhcp-active"
    if "-S" in arguments:
        if not active.exists():
            with (root / "dhcp-starts").open("a") as output:
                output.write("started\n")
            active.touch()
    elif active.exists():
        active.unlink()
elif name == "dmesg":
    print("fixture kernel boot diagnostics")
'''


class MikroStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ("proc/device-tree", "proc/sys/kernel/random", "sys/class/udc",
                          "sys/kernel/config/usb_gadget", "run", "etc/init.d", "bin"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "proc/device-tree/compatible").write_bytes(b"avnet,ultra96-rev1\0xlnx,zynqmp\0")
        (self.root / "proc/sys/kernel/random/uuid").write_text("01234567-89ab-cdef-0123-456789abcdef\n")
        (self.root / "proc/sys/kernel/random/boot_id").write_text("fixture-boot-id\n")
        for filename in ("etc/axi-lab-release", "proc/cmdline", "proc/mounts"):
            (self.root / filename).write_text("fixture boot input\n")
        self.environment = {**os.environ, "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
                            "FIXTURE_ROOT": str(self.root)}
        for name in ("mkdir", "ln", "mountpoint", "mount", "sleep", "ifconfig", "start-stop-daemon", "dmesg", "sync"):
            binary = self.root / "bin" / name
            binary.write_text(COMMAND)
            binary.chmod(0o755)
        for name in ("S30usbgadget", "rcS"):
            source = (OVERLAY / "etc/init.d" / name).read_text()
            source = re.sub(r"/(?:sys|proc|run|var|etc)/", lambda match: str(self.root) + match[0], source)
            target = self.root / name
            target.write_text(source)
            target.chmod(0o755)
        self.binding = self.root / "sys/kernel/config/usb_gadget/axi_lab/UDC"

    def controller(self, name="test.dwc3"):
        directory = self.root / "sys/class/udc" / name
        directory.mkdir()
        (directory / "state").write_text("not attached\n")

    def run_script(self, name="S30usbgadget", action="start", **environment):
        return subprocess.run([*SHELL, "-x", str(self.root / name), action], capture_output=True,
                              text=True, timeout=10, cwd=self.root, env={**self.environment, **environment})

    def commands(self):
        filename = self.root / "commands.log"
        return filename.read_text() if filename.exists() else ""

    def test_other_boards_are_untouched(self):
        (self.root / "proc/device-tree/compatible").write_bytes(b"linux,dummy-virt\0")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped", result.stdout)
        self.assertEqual(self.commands(), "")
        self.assertFalse(self.binding.exists())

    def test_late_controller_binds_and_initially_rebinds_without_host(self):
        result = self.run_script(CONTROLLER_DELAY="3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "sleeps").read_text(), "4")
        self.assertEqual(self.binding.read_text(), "test.dwc3\n")
        self.assertIn("stage=initial-bind attempt=1", result.stdout)
        self.assertIn("stage=initial-rebind attempt=1", result.stdout)
        self.assertIn("enumeration=NOT_VERIFIED", result.stdout)
        self.assertIn("not attached", result.stdout)
        self.assertEqual((self.binding.parent / "configs/c.1/ecm.usb0").resolve(),
                         self.binding.parent / "functions/ecm.usb0")
        self.assertNotIn("mass_storage", " ".join(str(path) for path in self.binding.parent.rglob("*")))
        self.assertIn("-S -q -o", self.commands())

    def test_no_controller_has_bounded_wait_and_failure_diagnostics(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "sleeps").read_text(), "30")
        self.assertIn("AXI_LAB_USB_FAIL stage=wait-controller", result.stderr)
        self.assertIn("fixture kernel boot diagnostics", result.stdout)
        self.assertNotIn("ifconfig", self.commands())

    def test_old_relative_links_fail_with_configfs_resolution_rules(self):
        self.controller()
        script = self.root / "S30usbgadget"
        source = script.read_text()
        source = source.replace('"$gadget/functions/ecm.usb0" "$gadget/configs/c.1/"',
                                '../../functions/ecm.usb0 "$gadget/configs/c.1/"')
        source = source.replace('"$gadget/functions/acm.usb0" "$gadget/configs/c.1/"',
                                '../../functions/acm.usb0 "$gadget/configs/c.1/"')
        script.write_text(source)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configfs target must resolve", result.stderr)
        self.assertNotIn("USB_BIND", result.stdout)
        self.assertEqual(self.binding.read_text(), "")

    def test_multiple_controllers_or_foreign_owners_are_not_reconfigured(self):
        self.controller()
        self.controller("other.dwc3")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to guess", result.stderr)
        (self.root / "sys/class/udc/other.dwc3/state").unlink()
        (self.root / "sys/class/udc/other.dwc3").rmdir()
        foreign = self.binding.parent.parent / "foreign/UDC"
        foreign.parent.mkdir()
        foreign.write_text("test.dwc3\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("leaving it intact", result.stderr)
        self.assertEqual(foreign.read_text(), "test.dwc3\n")
        self.assertFalse(self.binding.exists())

    def test_corrupt_persistent_identity_is_not_silently_replaced(self):
        self.controller()
        identity = self.root / "var/lib/axi-lab/gadget-id"
        identity.parent.mkdir(parents=True)
        for contents in ("", "invalid\n"):
            identity.write_text(contents)
            result = self.run_script()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid persistent gadget identity", result.stderr)
            self.assertEqual(identity.read_text(), contents)
            self.assertFalse(self.binding.exists())

    @unittest.skipIf(os.getuid() == 0, "write-permission fault requires an unprivileged test process")
    def test_bind_failure_retries_are_bounded_and_can_recover(self):
        self.controller()
        result = self.run_script(BIND_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count("USB_BIND stage=initial-bind"), 3)
        self.assertNotIn("ifconfig", self.commands())
        result = self.run_script(BIND_RECOVER="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USB_BIND stage=initial-bind attempt=2", result.stdout)
        self.assertIn("AXI_LAB_USB_READY", result.stdout)

    def test_repeated_start_preserves_connection_and_dhcp_and_stop_is_scoped(self):
        self.controller()
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        identity = (self.root / "var/lib/axi-lab/gadget-id").read_text()
        timestamp = self.binding.stat().st_mtime_ns
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("USB_BIND", second.stdout)
        self.assertEqual(self.binding.stat().st_mtime_ns, timestamp)
        self.assertEqual((self.root / "var/lib/axi-lab/gadget-id").read_text(), identity)
        self.assertEqual((self.root / "dhcp-starts").read_text(), "started\n")
        result = self.run_script(action="stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.binding.read_text(), "\n")
        self.assertFalse((self.root / "dhcp-active").exists())
        self.assertEqual(self.run_script(action="stop").returncode, 0)

    def test_network_failure_can_be_repaired_without_rebinding(self):
        self.controller()
        first = self.run_script(FAIL_COMMAND="start-stop-daemon")
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("AXI_LAB_USB_FAIL stage=network", first.stderr)
        self.assertNotIn("AXI_LAB_USB_READY", first.stdout)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("USB_BIND", result.stdout)
        self.assertTrue((self.root / "dhcp-active").exists())

    def test_operation_lock_prevents_competing_setup(self):
        self.controller()
        with (self.root / "run/axi-lab-usb.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already in progress", result.stderr)
        self.assertFalse(self.binding.exists())

    def test_init_records_failures_continues_and_rotates_private_logs(self):
        for name, contents in (("S10fail", "echo fixture-failure >&2\nexit 7\n"),
                               ("S20continue", "echo fixture-continued\n")):
            script = self.root / "etc/init.d" / name
            script.write_text("#!/bin/sh\n" + contents)
            script.chmod(0o755)
        result = self.run_script("rcS")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture-failure", result.stdout)
        self.assertIn("fixture-continued", result.stdout)
        log = self.root / "var/lib/axi-lab/init.log"
        contents = log.read_text()
        self.assertIn("AXI_LAB_INIT_RESULT service=S10fail status=7", contents)
        self.assertIn("AXI_LAB_INIT_RESULT service=S20continue status=0", contents)
        self.assertIn("BOOT_ID=fixture-boot-id", contents)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("fixture kernel", (log.parent / "boot-kernel.log").read_text())
        self.assertEqual(self.run_script("rcS").returncode, 0)
        self.assertEqual((log.parent / "init.log.previous").read_text(), contents)
        self.assertEqual(log.read_text(), contents)


if __name__ == "__main__":
    unittest.main()
