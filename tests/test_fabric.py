from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FabricTests(unittest.TestCase):
    def test_parameterized_transactors_and_protocol_rejections(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "fabric-test"
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O2",
                            "-I", str(ROOT / "sim"), str(ROOT / "tests/fabric_unit.cpp"), "-o", str(binary)],
                           check=True, timeout=60)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("FABRIC_UNIT_PASS concurrent_cases=80 negative_cases=12", result.stdout)
            self.assertIn("MONITOR_UNIT_PASS negative_cases=14", result.stdout)


if __name__ == "__main__":
    unittest.main()
