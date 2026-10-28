import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import framefx
import pl_dma


class PhysicalDmaTests(unittest.TestCase):
    def export(self, directory):
        profile = pl_dma.PROFILES["minimum"]
        for index in range(4):
            pixels = framefx.image(3, 3, 0, profile["seed"] + index, 127)
            output = framefx.transform(pixels, 3, 3, index, 127)
            record = dict(status="PASS", width=3, height=3, seed=profile["seed"] + index,
                          mode=index, threshold=127, source_stride=128, destination_stride=192,
                          source_bytes=8192, destination_bytes=8192, job_id=index + 7,
                          irq_count=index + 12, cycles=65, bytes_read=24, bytes_written=9, elapsed_ms=0.05)
            (directory / f"job-{index}.json").write_text(json.dumps(record))
            for kind, data in (("input", pixels), ("output", output)):
                (directory / f"{kind}-{index}.pgm").write_bytes(b"P5\n3 3\n255\n" + data)
            for kind, data, fill in (("source", pixels, 0xa5), ("destination", output, 0x5a)):
                buffer = bytearray([fill]) * record[f"{kind}_bytes"]
                for row in range(3):
                    start = row * record[f"{kind}_stride"]
                    buffer[start:start + 3] = data[row * 3:row * 3 + 3]
                (directory / f"{kind}-{index}.bin").write_bytes(buffer)

    def snapshot(self, count=12):
        return {"/proc/sys/kernel/random/boot_id": "2e6c2d97-82e4-4e31-8f92-b36752988ee9\n",
                "/proc/interrupts": f" 53: 0 {count} 0 0 GICv2 121 Level framefx\n",
                "/sys/module/framefx/refcnt": "0\n", "dmesg": "mtdoops: registered\n",
                "/proc/meminfo": "CmaFree: 124144 kB\n"}

    def test_independent_full_allocation_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.export(directory)
            result = pl_dma.validate_profile(directory, "minimum")
            self.assertEqual(result["jobs"], 4)
            self.assertEqual(len(result["artifact_sha256"]), 20)
            for name, offset in (("source-0.bin", 0), ("source-0.bin", 8191),
                                 ("destination-1.bin", 0), ("destination-1.bin", 3),
                                 ("output-2.pgm", 12)):
                path = directory / name
                original = path.read_bytes()
                corrupted = bytearray(original)
                corrupted[offset] ^= 1
                path.write_bytes(corrupted)
                with self.subTest(name=name, offset=offset), self.assertRaises(RuntimeError):
                    pl_dma.validate_profile(directory, "minimum")
                path.write_bytes(original)

    def test_missing_extra_symlink_and_trailing_exports_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.export(directory)
            path = directory / "source-0.bin"
            original = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(RuntimeError, "manifest"):
                pl_dma.validate_profile(directory, "minimum")
            path.symlink_to(directory / "source-1.bin")
            with self.assertRaisesRegex(RuntimeError, "manifest"):
                pl_dma.validate_profile(directory, "minimum")
            path.unlink()
            path.write_bytes(original + b"x")
            with self.assertRaisesRegex(RuntimeError, "full-allocation"):
                pl_dma.validate_profile(directory, "minimum")
            path.write_bytes(original)
            (directory / "unexpected").mkdir()
            with self.assertRaisesRegex(RuntimeError, "manifest"):
                pl_dma.validate_profile(directory, "minimum")

    def test_wrong_coverage_counters_and_elapsed_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.export(directory)
            path = directory / "job-1.json"
            original = json.loads(path.read_text())
            for key, value in (("irq_count", 12), ("job_id", 99), ("threshold", 128),
                               ("elapsed_ms", True), ("elapsed_ms", 0), ("elapsed_ms", float("nan")),
                               ("elapsed_ms", float("inf")), ("elapsed_ms", 1001), ("width", True),
                               ("bytes_written", 8), ("destination_bytes", 2**63)):
                path.write_text(json.dumps({**original, key: value}))
                with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                    pl_dma.validate_profile(directory, "minimum")
            path.write_text(json.dumps(original))
            with self.assertRaisesRegex(RuntimeError, "manifest"):
                pl_dma.validate_profile(directory, "copy-16")

    def test_exact_interrupt_delivery_and_ownership_required(self):
        baseline = self.snapshot()
        final = self.snapshot(16)
        self.assertEqual(pl_dma.validate_health(baseline, final, 4)["irq_delta"], 4)
        for changed in (" 53: 0 16 GICv2 121 Level fabric\n", " 53: 0 16 GICv2 121 Level framefx, other\n",
                        " 53: 0 16 GICv2 120 Level framefx\n", "", final["/proc/interrupts"] * 2):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                pl_dma.validate_health(baseline, {**final, "/proc/interrupts": changed}, 4)
        with self.assertRaisesRegex(RuntimeError, "Delivered interrupt"):
            pl_dma.validate_health(baseline, self.snapshot(17), 4)

    def test_same_boot_released_references_and_healthy_kernel_required(self):
        baseline, final = self.snapshot(), self.snapshot(16)
        for key, value in (("/proc/sys/kernel/random/boot_id", ""), ("/sys/module/framefx/refcnt", "1"),
                           ("dmesg", "rcu: detected stall"), ("dmesg", "SError Interrupt"),
                           ("dmesg", "FRAMEFX_QUARANTINE"), ("dmesg", "BUG: sleeping function"),
                           ("dmesg", "DMA-API: wrong size")):
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                pl_dma.validate_health(baseline, {**final, key: value}, 4)
        changed = copy.deepcopy(final)
        changed["/proc/meminfo"] = "CmaFree: 124140 kB\n"
        self.assertEqual(pl_dma.validate_health(baseline, changed, 4)["exact_cma_balance"], "NOT_MEASURED")


if __name__ == "__main__":
    unittest.main()
