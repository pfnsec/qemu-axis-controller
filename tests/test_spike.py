import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import spike

spec = importlib.util.spec_from_file_location("integrate", ROOT / "qemu/integrate.py")
integrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integrate)


class SpikeEvidenceTests(unittest.TestCase):
    def fixture(self):
        lines = ["AXI_DMA kind=4 id=2 status=0 pending=1 bql=1 thread=12",
                 "AXI_REPLY kind=3 id=11 address=0x8 error=0",
                 "AXI_DMA kind=5 id=3 status=0 pending=0 bql=1 thread=10"]
        lines.extend(f"SPIKE_CASE test={index} PASS" for index in range(36))
        lines.extend(f"SPIKE_CASE test={index} burst bytes={size} checked PASS"
                     for index, size in enumerate((128, 136, 264, 2056), 36))
        lines.extend("AXI_DMA kind=4 status=2 pending=1 bql=1 thread=12" for _ in range(4))
        lines.extend(["SPIKE_RESULT result=0 tests=40 irqs=40", "AXI_LAB_SPIKE_PASS", "reboot: Power down"])
        return "\n".join(lines)

    def test_concurrency_evidence_is_required(self):
        console = self.fixture()
        simulator = "RTL_SUMMARY cycles=1 reads=61 writes=59 irqs=40 mmios=1 max_read=16 max_write=16\nRTL_DMA kind=4 bytes=128\nRTL_DMA kind=5 bytes=128"
        spike.validate(console, simulator, "none")
        for pattern in ("AXI_DMA kind=4 id=2 status=0 pending=1 bql=1 thread=12", "pending=0", "test=17 ",
                        "SPIKE_RESULT", "AXI_LAB_SPIKE_PASS", "reboot: Power down", "thread=12"):
            with self.subTest(pattern=pattern), self.assertRaises(RuntimeError):
                spike.validate(console.replace(pattern, "missing"), simulator, "none")
        with self.assertRaises(RuntimeError):
            spike.validate(console + "\nAXI_COSIM_FAULT", simulator, "none")
        with self.assertRaises(RuntimeError):
            spike.validate(console, "", "none")

    def test_fault_must_be_bounded_and_fail_closed(self):
        console = "AXI_COSIM_FAULT\nAXI_LAB_SPIKE_FAIL\nreboot: Power down"
        spike.validate(console, "RTL_FAILURE injected", "stall")
        for extra in ("\nAXI_LAB_SPIKE_PASS", "\nAXI_DMA kind=5", "\nKernel panic"):
            with self.assertRaises(RuntimeError):
                spike.validate(console + extra, "", "stall")
        with self.assertRaises(RuntimeError):
            spike.validate("AXI_LAB_SPIKE_FAIL", "", "stall")


class IntegrationTests(unittest.TestCase):
    def test_anchored_hook_is_idempotent_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "hook"
            target.write_text("before\n")
            integrate.replace_once(target, "before", "prefix before")
            integrate.replace_once(target, "before", "prefix before")
            self.assertEqual(target.read_text(), "prefix before\n")
            target.write_text("unrecognized\n")
            with self.assertRaises(RuntimeError):
                integrate.replace_once(target, "before", "prefix before")
            self.assertEqual(target.read_text(), "unrecognized\n")

    def test_wrong_qemu_version_rejected_before_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "VERSION").write_text("0.0.0\n")
            with self.assertRaisesRegex(RuntimeError, "version"):
                integrate.integrate(source)
