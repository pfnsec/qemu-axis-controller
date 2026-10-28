from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hardening
import lab


class HardeningTests(unittest.TestCase):
    def test_strict_simulator_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "options"
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O2",
                            "-I", str(lab.ROOT / "sim"), "-I", str(lab.ROOT / "transport"),
                            str(lab.ROOT / "tests/options_unit.cpp"), "-o", str(binary)], check=True, timeout=60)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, check=True)
            self.assertIn("OPTIONS_UNIT_PASS", result.stdout)

    def test_control_application_compiles_strictly(self):
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{lab.ROOT / 'linux/uapi'}",
                            str(lab.ROOT / "linux/framefx/app/framefx-control-test.c"), "-o", str(Path(temporary) / "control")],
                           check=True, timeout=30)

    def test_terminal_fault_evidence_rejects_missing_or_post_fault_dma(self):
        console = ("FRAMEFX_CONTROL_PASS operation=submit\nFRAMEFX_KERNEL_DEBUG_PASS\n"
                   "FRAMEFX_CONTROL_QUARANTINE_PASS\nFRAMEFX_CONTROL_CMA allocated_pages=4 released_pages=0\n"
                   "AXI_COSIM_FAULT\nFRAMEFX_QUARANTINE\n")
        simulator = "RTL_INJECTED_MMIO\n"
        self.assertTrue(hardening.validate_control(console, simulator, "submit", True)["terminal"])
        for marker in ("FRAMEFX_CONTROL_PASS", "FRAMEFX_KERNEL_DEBUG_PASS", "FRAMEFX_QUARANTINE", "released_pages=0"):
            with self.subTest(marker=marker), self.assertRaises(RuntimeError):
                hardening.validate_control(console.replace(marker, ""), simulator, "submit", True)
        for altered, trace in ((console + "AXI_DMA ", simulator), (console, ""), (console, simulator * 2)):
            with self.assertRaises(RuntimeError):
                hardening.validate_control(altered, trace, "submit", True)

    def test_corrupt_telemetry_requires_safe_reclamation(self):
        console = ("FRAMEFX_CONTROL_PASS operation=wait\nFRAMEFX_KERNEL_DEBUG_PASS\nFRAMEFX_CONTROL_RELOAD_PASS\n"
                   "FRAMEFX_CONTROL_CMA allocated_pages=4 released_pages=4\n")
        self.assertFalse(hardening.validate_control(console, "RTL_INJECTED_MMIO", "wait", False)["terminal"])
        for marker in ("AXI_COSIM_FAULT", "FRAMEFX_QUARANTINE", "FRAMEFX_CONTROL_FAIL"):
            with self.subTest(marker=marker), self.assertRaises(RuntimeError):
                hardening.validate_control(console + marker, "RTL_INJECTED_MMIO", "wait", False)

    def test_runtime_compat_requires_all_evidence(self):
        console = ("FRAMEFX_COMPAT32_PASS jobs=8 modes=4 pointer_bits=32\nFRAMEFX_KERNEL_DEBUG_PASS\n"
                   "FRAMEFX_CONTROL_RELOAD_PASS\nFRAMEFX_CONTROL_CMA allocated_pages=4 released_pages=4\n")
        self.assertEqual(hardening.validate_compat(console, "RTL_SUMMARY")["images"], 8)
        for marker in console.splitlines():
            with self.subTest(marker=marker), self.assertRaises(RuntimeError):
                hardening.validate_compat(console.replace(marker, ""), "RTL_SUMMARY")


if __name__ == "__main__":
    unittest.main()
