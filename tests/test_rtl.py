import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab
import rtl


class RtlEvidenceTests(unittest.TestCase):
    def fixture(self, directory):
        case_id = 3
        case = dict(rtl.expected_cases()[case_id], id=case_id, seed=1, fill_seed=1, status="PASS",
                    snapshot=f"case-{case_id}.bin", reads=1, writes=1, read_beats=1, write_beats=1,
                    max_read=1, max_write=1, cycles=400)
        original = bytes(((offset * 37 + 17) ^ (offset >> 8)) & 255 for offset in range(65536))
        memory = bytearray(original)
        mask = bytes.fromhex("f4e59687a0b1c2d3")
        for lane, value in enumerate(mask):
            memory[0x4040 + lane] = original[64 + lane] ^ value
        (directory / case["snapshot"]).write_bytes(memory)
        (directory / "cases.jsonl").write_text(json.dumps(case) + "\n")
        (directory / "events.log").write_text(f"3 1 AR address={0x100000040} beats=1\n3 2 AW address={0x100004040} beats=1\n")
        return case

    def test_independent_full_ram_scoreboard(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            case = self.fixture(directory)
            self.assertEqual(rtl.validate(directory, 1, 3), [case])
            snapshot = directory / case["snapshot"]
            original = snapshot.read_bytes()
            for offset in (0, 64, 0x4040, 65535):
                changed = bytearray(original)
                changed[offset] ^= 1
                snapshot.write_bytes(changed)
                with self.subTest(offset=offset), self.assertRaises(rtl.CaseFailure) as raised:
                    rtl.validate(directory, 1, 3)
                self.assertEqual(raised.exception.case_id, 3)
            snapshot.write_bytes(original[:-1])
            with self.assertRaisesRegex(rtl.CaseFailure, "RAM scoreboard"):
                rtl.validate(directory, 1, 3)
            snapshot.unlink()
            with self.assertRaisesRegex(rtl.CaseFailure, "Cannot read snapshot"):
                rtl.validate(directory, 1, 3)

    def test_metadata_and_bus_evidence_cannot_be_faked(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for field, value in (("status", "FAIL"), ("written", 0), ("reads", 2), ("read_beats", 0),
                                 ("max_write", 17), ("fill_seed", 2), ("snapshot", "elsewhere.bin")):
                case = self.fixture(directory)
                case[field] = value
                (directory / "cases.jsonl").write_text(json.dumps(case) + "\n")
                with self.subTest(field=field), self.assertRaises(rtl.CaseFailure):
                    rtl.validate(directory, 1, 3)
            case = self.fixture(directory)
            (directory / "events.log").write_text("")
            with self.assertRaisesRegex(rtl.CaseFailure, "Burst/address"):
                rtl.validate(directory, 1, 3)
            (directory / "cases.jsonl").write_text((json.dumps(case) + "\n") * 2)
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                rtl.validate(directory, 1, 3)

    def test_plan_and_independent_boundary_splitting(self):
        self.assertEqual(len(rtl.expected_cases()), 346)
        self.assertEqual(rtl.bursts(0xff8, 0x2ff0, 136),
                         [(0xff8, 0x2ff0, 1), (0x1000, 0x2ff8, 1), (0x1008, 0x3000, 15)])
        for source in (0, 8, 0xf80, 0xff8):
            for destination in (0x4000, 0x4fc0, 0x4ff8):
                transfers = rtl.bursts(source, destination, 257 * 8)
                self.assertEqual(sum(count for _, _, count in transfers), 257)
                for read, write, count in transfers:
                    self.assertTrue(1 <= count <= 16)
                    self.assertLessEqual(read % 4096 + count * 8, 4096)
                    self.assertLessEqual(write % 4096 + count * 8, 4096)

    def test_failure_replays_offending_case_not_last_completed(self):
        calls = []

        def logged(command, log, timeout):
            calls.append(command)
            count = 1 if len(command) == 4 else 346
            log.write_text(f"RTL_CASE_BEGIN id=345\nRTL_SUITE_PASS seed=1 cases={count}\n")

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(lab, "logged", logged), \
                mock.patch.object(rtl, "validate", side_effect=[rtl.CaseFailure(3, "corrupt guard"), []]):
            directory = Path(temporary) / "run"
            with self.assertRaisesRegex(rtl.CaseFailure, "corrupt guard"):
                rtl.run(directory, 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][-1], "3")
            self.assertEqual(calls[1][-2], directory / "failure-trace")


class EvidenceFreshnessTests(unittest.TestCase):
    def test_missing_stale_failed_and_changed_binary_are_not_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / "binary"
            binary.write_bytes(b"original")
            project = {"input": "digest"}
            record = {"status": "PASS", "directory": str(directory)}
            self.assertFalse(lab.evidence_current(record, project))
            versions = {"project_sha256": project, "binary_sha256": {str(binary): lab.sha256(binary)}}
            lab.write_json(directory / "versions.json", versions)
            self.assertTrue(lab.evidence_current(record, project))
            self.assertFalse(lab.evidence_current(record, {"input": "changed"}))
            self.assertFalse(lab.evidence_current(dict(record, status="FAIL"), project))
            binary.write_bytes(b"changed")
            self.assertFalse(lab.evidence_current(record, project))
            binary.unlink()
            self.assertFalse(lab.evidence_current(record, project))
            (directory / "versions.json").write_text("{}")
            self.assertFalse(lab.evidence_current(record, project))
