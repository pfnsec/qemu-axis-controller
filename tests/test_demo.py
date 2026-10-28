import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import demo
import framefx
import lab


class UserspaceTests(unittest.TestCase):
    def record(self):
        return dict(width=3, height=3, seed=1, mode=0, threshold=127, source_stride=64,
                    destination_stride=128, source_bytes=4096, destination_bytes=4096,
                    bytes_read=24, bytes_written=9, status="PASS", job_id=1, cycles=65, irq_count=1)

    def test_c_reference_matches_independent_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "reference"
            subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            f"-I{lab.ROOT / 'linux/uapi'}", str(lab.ROOT / "tests/framefx_reference.c"), "-o", str(binary)], check=True)
            for width, height, seed in ((3, 3, 1), (65, 9, 2026), (320, 240, 42), (7, 13, 0xffffffff)):
                for mode in range(4):
                    for threshold in (0, 127, 255):
                        actual = subprocess.check_output([str(binary), str(width), str(height), str(seed), str(mode), str(threshold)])
                        pixels = framefx.image(width, height, 0, seed, threshold)
                        self.assertEqual(actual, framefx.transform(pixels, width, height, mode, threshold))

    def test_uapi_layouts_32_and_64(self):
        with tempfile.TemporaryDirectory() as temporary:
            for bits in (32, 64):
                subprocess.run(["gcc", f"-m{bits}", "-std=c11", "-Wall", "-Werror", "-c",
                                str(lab.ROOT / "tests/framefx_uapi.c"), "-o", str(Path(temporary) / f"uapi{bits}.o")], check=True)

    def test_missing_images_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "Expected 28 images"):
                demo.validate_images(Path(temporary))

    def test_corrupt_export_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "small"
            directory.mkdir()
            (directory / "job-0.json").write_text(json.dumps(self.record()))
            (directory / "input-0.pgm").write_bytes(b"P5\n3 3\n255\n" + bytes(9))
            with self.assertRaisesRegex(RuntimeError, "Independent PGM comparison failed"):
                demo.validate_images(Path(temporary))

    def test_padding_and_counter_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "reload"
            directory.mkdir()
            record = dict(self.record(), mode=3)
            pixels = framefx.image(3, 3, 0, 1, 127)
            output = framefx.transform(pixels, 3, 3, 3, 127)
            for kind, data in (("input", pixels), ("output", output)):
                (directory / f"{kind}-0.pgm").write_bytes(b"P5\n3 3\n255\n" + data)
            for kind, data, fill in (("source", pixels, 0xa5), ("destination", output, 0x5a)):
                buffer = bytearray([fill]) * record[f"{kind}_bytes"]
                stride = record[f"{kind}_stride"]
                for row in range(3):
                    buffer[row * stride:row * stride + 3] = data[row * 3:row * 3 + 3]
                (directory / f"{kind}-0.bin").write_bytes(buffer)
            filename = directory / "job-0.json"
            filename.write_text(json.dumps(record))
            with self.assertRaisesRegex(RuntimeError, "Expected 28 images"):
                demo.validate_images(Path(temporary))
            snapshot = directory / "destination-0.bin"
            original = snapshot.read_bytes()
            corrupted = bytearray(original)
            corrupted[3] ^= 1
            snapshot.write_bytes(corrupted)
            with self.assertRaisesRegex(RuntimeError, "Independent full-allocation comparison failed"):
                demo.validate_images(Path(temporary))
            snapshot.write_bytes(original)
            record["bytes_written"] = 8
            filename.write_text(json.dumps(record))
            with self.assertRaisesRegex(RuntimeError, "Incorrect counters"):
                demo.validate_images(Path(temporary))

    def test_malformed_metadata_rejected_before_transform(self):
        for key, value in (("width", 2**63), ("height", -1), ("seed", True), ("width", 3.0),
                           ("mode", 4), ("threshold", 256), ("source_stride", 1),
                           ("destination_stride", 2**21), ("source_bytes", 2**63),
                           ("destination_bytes", 4097), ("cycles", 0), ("irq_count", "1"),
                           ("bytes_written", 10), ("status", "FAIL")):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "reload"
                directory.mkdir()
                record = dict(self.record(), **{key: value})
                (directory / "job-0.json").write_text(json.dumps(record))
                with patch.object(framefx, "image", side_effect=AssertionError("must validate before allocation")):
                    with self.assertRaises(RuntimeError):
                        demo.validate_images(Path(temporary))

    def test_invalid_json_and_oversized_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "reload"
            directory.mkdir()
            for encoded in (b"{" * 4097, b"[]", b"null", b"{}", b"\xff", b"{"):
                (directory / "job-0.json").write_bytes(encoded)
                with self.subTest(encoded=encoded[:8]), self.assertRaisesRegex(RuntimeError, "metadata"):
                    demo.validate_images(Path(temporary))
            binary = directory / "data.bin"
            with binary.open("wb") as stream:
                stream.truncate(2**32)
            self.assertFalse(demo.matches_file(binary, b"\x00" * 4096))

    def test_guest_cannot_override_evidence_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "unexpected"
            directory.mkdir()
            claimed = [f"{profile}/job-{index}.json" for profile, count in
                       (("small", 4), ("medium", 4), ("narrow", 12), ("maximum", 4), ("reload", 4)) for index in range(count)]
            for index, name in enumerate(claimed):
                (directory / f"job-{index}.json").write_text(json.dumps(dict(self.record(), file=name)))
            with patch.object(demo, "matches_file", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "Incorrect image manifest"):
                    demo.validate_images(Path(temporary))


if __name__ == "__main__":
    unittest.main()
