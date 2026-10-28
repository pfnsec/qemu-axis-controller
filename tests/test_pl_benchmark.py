import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab
import pl_benchmark


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temporary.name) / "fixture"
        command = ["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                   f"-I{lab.ROOT / 'linux/uapi'}", str(lab.ROOT / "tests/framefx_bench_fixture.c"), "-o", str(cls.binary)]
        subprocess.run(command + [f"-Wl,--wrap={name}" for name in ("open", "close", "mmap", "munmap", "ioctl", "write")], check=True)
        subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                        f"-I{lab.ROOT / 'linux/uapi'}", str(lab.ROOT / "linux/framefx/app/framefx-bench.c"),
                        "-o", str(Path(cls.temporary.name) / "native")], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_fixture(self, directory, width=7, height=5, mode=3, contend=0, fault=None):
        environment = os.environ.copy()
        if fault:
            environment["FRAMEFX_BENCH_FIXTURE_FAULT"] = fault
        result = subprocess.run([str(self.binary), str(width), str(height), str(mode), "8", "4", str(contend), str(directory)],
                                capture_output=True, env=environment, timeout=10)
        (directory / "benchmark.jsonl").write_bytes(result.stdout)
        return result

    def test_independent_full_buffer_checks_all_modes_and_contention(self):
        for width, height in ((3, 3), (65, 9), (513, 17)):
            for mode in range(4):
                with self.subTest(width=width, mode=mode), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    result = self.run_fixture(directory, width, height, mode, 1)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = pl_benchmark.validate(directory, dict(width=width, height=height, mode=mode), 100000000)
                    self.assertEqual((report["jobs"], report["measured_jobs"]), (12, 8))
                    self.assertEqual(report["irq_last"] - report["irq_first"], 11)
                    self.assertEqual(report["hardware_ns"]["median"], 10000)

    def test_native_failure_stops_without_reset_or_retry(self):
        for fault in ("guard", "counter", "wait"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                result = self.run_fixture(directory, fault=fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"FRAMEFX_BENCH_FAIL", result.stderr)
                self.assertNotIn(b'"type":"sample"', result.stdout)
                self.assertFalse((directory / "source.bin").exists())

    def test_cli_bounds_before_device_access(self):
        valid = ["3", "3", "0", "1", "0", "0", "/nonexistent"]
        for index, value in ((0, "-3"), (0, "+3"), (0, " 3"), (0, "3x"), (0, "1025"),
                             (1, "2"), (2, "4"), (3, "0"), (3, "257"), (4, "17"),
                             (5, "2"), (0, "99999999999999999999999999")):
            arguments = valid.copy()
            arguments[index] = value
            result = subprocess.run([str(Path(self.temporary.name) / "native"), *arguments], capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"numeric argument", result.stderr)

    def test_statistics_units_and_warmup_exclusion(self):
        self.assertEqual(pl_benchmark.distribution(range(1, 101))["p95"], 95)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(self.run_fixture(directory).returncode, 0)
            records = [json.loads(line) for line in (directory / "benchmark.jsonl").read_text().splitlines()]
            for index, sample in enumerate(records[1:-1]):
                sample.update(submit_ns=1000, wait_ns=999000 if index >= 4 else 999999000,
                              elapsed_ns=1000000 if index >= 4 else 1000000000)
            (directory / "benchmark.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
            report = pl_benchmark.validate(directory)
            self.assertEqual(report["timings"]["elapsed_ns"]["median"], 1000000)
            self.assertEqual(report["jobs_per_active_second"], 1000)
            self.assertAlmostEqual(report["payload_mib_per_second"], 35 * 1000 / 2**20)
            self.assertAlmostEqual(report["counted_dma_mib_per_second"], 75 * 1000 / 2**20)

    def test_corrupt_or_unbounded_metadata_and_snapshots_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(self.run_fixture(directory).returncode, 0)
            filename = directory / "benchmark.jsonl"
            original = filename.read_text()
            records = [json.loads(line) for line in original.splitlines()]
            for index, key, value in ((0, "width", 2**63), (0, "samples", True), (0, "caps_ns", [1]),
                                       (0, "source_bytes", 2**63), (1, "elapsed_ns", float("nan")),
                                       (1, "bytes_read", 35), (1, "destination_crc32", 0), (1, "seed", 1),
                                       (2, "job_id", 1), (-1, "jobs", 13), (-1, "ownership_released", 1),
                                       (-1, "control_queries", 1)):
                damaged = copy.deepcopy(records)
                damaged[index][key] = value
                filename.write_text("".join(json.dumps(record) + "\n" for record in damaged))
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    pl_benchmark.validate(directory)
            for data in (original[:-2], original + "{}\n", "{" * 4097, "null\n", "{}\n" * 275):
                filename.write_text(data)
                with self.assertRaises(RuntimeError):
                    pl_benchmark.validate(directory)
            filename.write_text(original)
            snapshot = directory / "destination.bin"
            buffer = bytearray(snapshot.read_bytes())
            buffer[-1] ^= 1
            snapshot.write_bytes(buffer)
            with self.assertRaisesRegex(RuntimeError, "snapshot mismatch"):
                pl_benchmark.validate(directory)
            snapshot.unlink()
            snapshot.symlink_to(directory / "source.bin")
            with patch.object(pl_benchmark, "reference_buffers", side_effect=AssertionError("must reject before allocation")):
                with self.assertRaises(RuntimeError):
                    pl_benchmark.validate(directory)
