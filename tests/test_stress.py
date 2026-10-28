import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import framefx
import lab
import stress


class StressTests(unittest.TestCase):
    def test_random_plan_repeatability_bounds_and_coverage(self):
        plans = stress.random_cases(512, 2026)
        self.assertEqual(plans, stress.random_cases(512, 2026))
        self.assertNotEqual(plans, stress.random_cases(512, 42))
        self.assertEqual({plan["maximum"] for plan in plans}, {1, 4, 16})
        self.assertEqual({plan["case"]["width"] % 8 for plan in plans}, set(range(8)))
        self.assertEqual({plan["case"]["pattern"] for plan in plans}, set(range(7)))
        self.assertEqual({plan["case"]["error"] for plan in plans}, {0, 2, 3})
        for plan in plans[:30]:
            case = plan["case"]
            original, expected, _, counters, events, _, _ = framefx.prepare(case, plan["seed"], plan["maximum"])
            self.assertEqual(len(original), len(expected))
            self.assertEqual(counters["read_bytes"], sum(beats * 8 for kind, _, beats in events if kind == "AR"))
            for _, address, beats in events:
                self.assertEqual(address % 8, 0)
                self.assertLessEqual((address % 4096) + beats * 8, 4096)
                self.assertLessEqual(beats, plan["maximum"])
            if case["error"]:
                self.assertEqual(counters["reads" if case["error"] == 2 else "writes"], case["fail_read" if case["error"] == 2 else "fail_write"])

    def test_random_plan_rejects_invalid_budgets(self):
        for count, seed in ((0, 1), (100001, 1), (1, 0), (1, 2**32)):
            with self.assertRaises(ValueError):
                stress.random_cases(count, seed)

    def fixture(self, directory):
        records = []
        snapshots = bytearray()
        for plan in stress.linux_cases(4, 2026):
            width, height = plan["width"], plan["height"]
            pixels = framefx.image(width, height, 0, plan["seed"], plan["threshold"])
            output = framefx.transform(pixels, width, height, plan["mode"], plan["threshold"])
            snapshot = bytearray([0x5a]) * stress.BUFFER_BYTES
            for row in range(height):
                offset = row * plan["destination_stride"]
                snapshot[offset:offset + width] = output[row * width:(row + 1) * width]
            snapshots.extend(snapshot)
            records.append(dict(plan, job_id=plan["index"] + 2, cycles=123,
                                bytes_read=((width + 7) & ~7) * height, bytes_written=width * height, irq_count=plan["index"] + 2))
        (directory / "stress.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
        (directory / "destination.bin").write_bytes(snapshots)
        (directory / "lockdep.txt").write_text(" debug_locks: 1\n")
        console = ("FRAMEFX_STRESS_PASS jobs=4 seed=2026 cpus=4 workers=4 operations=56 owners=1\n"
                   "FRAMEFX_KILLED_OWNER_PASS\nFRAMEFX_KERNEL_DEBUG_PASS\nFRAMEFX_CMA_PASS\nFRAMEFX_ACTIVE_LIFETIME_PASS\n"
                   "FRAMEFX_CMA allocated_pages=64 released_pages=64\n")
        return console, records, snapshots

    def test_linux_evidence_valid_and_compressed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            console, _, snapshots = self.fixture(directory)
            self.assertEqual(stress.validate_linux(directory, console, 4, 2026, 4)["images"], 4)
            with gzip.open(directory / "destination.bin.gz", "wb") as stream:
                stream.write(snapshots)
            (directory / "destination.bin").unlink()
            self.assertEqual(stress.validate_linux(directory, console, 4, 2026, 4)["images"], 4)

    def test_linux_evidence_corruption_cannot_pass(self):
        for corruption, diagnostic in (("pixel", "full-buffer"), ("padding", "full-buffer"), ("truncate", "full-buffer"),
                                       ("extra", "snapshot length"), ("counter", "completion"), ("seed", "seeded plan"),
                                       ("count", "image count"), ("cma", "CMA accounting"), ("lockdep", "checker disabled"),
                                       ("cpus", "Missing SMP"), ("fault", "failure/quarantine")):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                console, records, snapshots = self.fixture(directory)
                if corruption == "pixel": snapshots[0] ^= 1
                if corruption == "padding": snapshots[records[0]["width"]] ^= 1
                if corruption == "truncate": snapshots.pop()
                if corruption == "extra": snapshots.append(0)
                if corruption == "counter": records[0]["bytes_read"] += 8
                if corruption == "seed": records[0]["seed"] ^= 1
                if corruption == "count": records.pop()
                if corruption == "cma": console = console.replace("released_pages=64", "released_pages=63")
                if corruption == "lockdep": (directory / "lockdep.txt").write_text(" debug_locks: 0\n")
                if corruption == "cpus": console = console.replace("cpus=4", "cpus=1")
                if corruption == "fault": console += "AXI_COSIM_FAULT"
                (directory / "stress.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
                (directory / "destination.bin").write_bytes(snapshots)
                with self.assertRaisesRegex(RuntimeError, diagnostic):
                    stress.validate_linux(directory, console, 4, 2026, 4)

    def test_stress_metadata_is_bounded_and_typed(self):
        for corruption in ("oversized", "list", "invalid-json", "counter-string", "counter-bool", "counter-overflow", "index-bool"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                console, records, _ = self.fixture(directory)
                if corruption == "counter-string": records[0]["cycles"] = "123"
                if corruption == "counter-bool": records[0]["irq_count"] = True
                if corruption == "counter-overflow": records[0]["cycles"] = 2**64
                if corruption == "index-bool": records[0]["index"] = False
                encoded = "".join(json.dumps(record) + "\n" for record in records)
                if corruption == "oversized": encoded = " " * 4097 + encoded
                if corruption == "list": encoded = "[]\n" + encoded
                if corruption == "invalid-json": encoded = "{\n" + encoded
                (directory / "stress.jsonl").write_text(encoded)
                with self.assertRaises(RuntimeError):
                    stress.validate_linux(directory, console, 4, 2026, 4)

    def test_stress_application_compiles_strictly(self):
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                            f"-I{lab.ROOT / 'linux/uapi'}", str(lab.ROOT / "linux/framefx/app/framefx-stress.c"),
                            "-o", str(Path(temporary) / "stress")], check=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
