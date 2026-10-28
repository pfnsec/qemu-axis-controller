import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import framefx


class GoldenImageTests(unittest.TestCase):
    def test_copy_invert_and_threshold_endpoints(self):
        pixels = bytes(range(256))
        self.assertEqual(framefx.transform(pixels, 256, 1, 0, 0), pixels)
        self.assertEqual(framefx.transform(pixels, 256, 1, 1, 0), bytes(reversed(range(256))))
        for threshold in (0, 1, 127, 128, 254, 255):
            result = framefx.transform(pixels, 256, 1, 2, threshold)
            self.assertEqual(result, bytes(threshold) + bytes([255]) * (256 - threshold))

    def test_sobel_signed_scale_saturation_and_zero_borders(self):
        ramp = bytes([0, 3, 6, 7, 10, 13, 14, 17, 20])
        expected = bytes([0, 0, 0, 0, 20, 0, 0, 0, 0])
        self.assertEqual(framefx.transform(ramp, 3, 3, 3, 0), expected)
        self.assertEqual(framefx.transform(bytes(255 - value for value in ramp), 3, 3, 3, 0), expected)
        transpose = bytes(ramp[column * 3 + row] for row in range(3) for column in range(3))
        self.assertEqual(framefx.transform(transpose, 3, 3, 3, 0), expected)
        corner = bytes([0, 0, 255, 0, 0, 255, 255, 255, 255])
        self.assertEqual(framefx.transform(corner, 3, 3, 3, 0), bytes([0, 0, 0, 0, 255, 0, 0, 0, 0]))
        impulse = bytes([0, 255, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(framefx.transform(impulse, 3, 3, 3, 0)[4], 127)
        for value in (0, 128, 255):
            self.assertEqual(framefx.transform(bytes([value]) * 35, 7, 5, 3, 0), bytes(35))

    def test_reference_rejects_inconsistent_input(self):
        for args in ((b"", 1, 1, 0, 0), (b"", 0, 0, 0, 0), (b"x", 1, 1, 4, 0)):
            with self.assertRaises(ValueError):
                framefx.transform(*args)


class FramePlanTests(unittest.TestCase):
    def test_row_boundary_and_tail_strobes(self):
        self.assertEqual(list(framefx.row_bursts(0xfc0, 257, 16)),
                         [(0xfc0, 8, 64), (0x1000, 16, 128), (0x1080, 9, 65)])
        for maximum in (1, 4, 16):
            transfers = list(framefx.row_bursts(0xfc0, 1023, maximum))
            self.assertEqual(sum(enabled for _, _, enabled in transfers), 1023)
            self.assertEqual(sum(beats * 8 for _, beats, _ in transfers), 1024)
            for address, beats, _ in transfers:
                self.assertLessEqual(address % 4096 + beats * 8, 4096)
                self.assertTrue(1 <= beats <= maximum)

    def test_invalid_configuration_never_reaches_memory(self):
        for case in framefx.cases():
            if case["error"] != 1:
                continue
            original, expected, _, counters, events, _, _ = framefx.prepare(case, 1, 16)
            self.assertEqual(original, expected)
            self.assertEqual(events, [])
            self.assertEqual(counters["reads"], 0)
            self.assertEqual(counters["writes"], 0)

    def test_sobel_reads_each_row_once_and_preserves_padding(self):
        case = next(case for case in framefx.cases() if case["mode"] == 3 and case["width"] == 65 and case["height"] == 9)
        original, expected, configuration, counters, _, _, destination = framefx.prepare(case, 1, 16)
        self.assertEqual(counters["read_bytes"], 72 * 9)
        self.assertEqual(counters["written_bytes"], 65 * 9)
        allowed = {destination + row * case["destination_stride"] + column for row in range(9) for column in range(65)}
        self.assertTrue(all(before == after for offset, (before, after) in enumerate(zip(original, expected)) if offset not in allowed))
        self.assertGreaterEqual(configuration[6], 1 << 32)

    def test_read_and_write_faults_keep_only_acknowledged_prefix(self):
        plans = framefx.cases()
        read = next(case for case in plans if case["name"] == "read-error-1")
        original, expected, _, counters, _, _, _ = framefx.prepare(read, 42, 16)
        self.assertEqual(original, expected)
        self.assertEqual(counters["written_bytes"], 0)
        self.assertGreater(counters["read_bytes"], 0)
        write = next(case for case in plans if case["name"] == "write-error-2")
        original, expected, _, counters, events, _, destination = framefx.prepare(write, 42, 16)
        first_write = next(event for event in events if event[0] == "AW")
        self.assertEqual(counters["written_bytes"], first_write[2] * 8)
        self.assertEqual(expected[destination + counters["written_bytes"]:], original[destination + counters["written_bytes"]:])

    def test_evidence_rejects_false_success_and_replays(self):
        case = framefx.cases()[0]
        prepared = framefx.prepare(case, 1, 16)
        for corruption in (None, "ram", "counter", "address", "marker"):
            calls = []

            def logged(command, logfile, timeout):
                calls.append(command)
                directory = command[1]
                if command[-1] == "trace":
                    raise RuntimeError("diagnostic fixture")
                actual = bytearray(prepared[1])
                if corruption == "ram":
                    actual[0] ^= 1
                (directory / "actual.bin").write_bytes(actual)
                result = dict(prepared[3], status="PASS", cycles=10)
                if corruption == "counter":
                    result["read_bytes"] += 8
                (directory / "rtl.json").write_text(json.dumps(result))
                events = "".join(f"{index} {kind} address={address} beats={beats}\n" for index, (kind, address, beats) in enumerate(prepared[4]))
                (directory / "events.log").write_text("" if corruption == "address" else events)
                logfile.write_text("missing" if corruption == "marker" else "FRAMEFX_PASS\n")

            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary, \
                    mock.patch.object(framefx.lab, "logged", logged):
                directory = Path(temporary) / "case"
                if corruption is None:
                    self.assertEqual(framefx.run_case(directory, Path("fake-binary"), case, 1, 16)["status"], "PASS")
                else:
                    with self.assertRaises(RuntimeError):
                        framefx.run_case(directory, Path("fake-binary"), case, 1, 16)
                    self.assertEqual(calls[-1][-1], "trace")
