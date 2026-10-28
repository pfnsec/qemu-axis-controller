import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab


class ArchiveTests(unittest.TestCase):
    def test_corrupt_download_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "download"
            archive.write_bytes(b"abc")
            lab.verify_archive(archive, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
            archive.write_bytes(b"corrupted")
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                lab.verify_archive(archive, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def make_archive(self, directory, name, link=None):
        archive = directory / "source.tar"
        with tarfile.open(archive, "w") as bundle:
            member = tarfile.TarInfo(name)
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = link
                bundle.addfile(member)
            else:
                member.size = 4
                bundle.addfile(member, io.BytesIO(b"data"))
        return archive

    def test_expected_root_extracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = self.make_archive(directory, "source/file")
            lab.extract_archive(archive, directory / "source", "source")
            self.assertEqual((directory / "source/file").read_bytes(), b"data")

    def test_traversal_and_unexpected_root_rejected(self):
        for name in ("source/../escape", "/tmp/escape", "other/file"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                archive = self.make_archive(directory, name)
                with self.assertRaises(RuntimeError):
                    lab.extract_archive(archive, directory / "source", "source")
                self.assertFalse((directory / "source").exists())

    def test_symlink_escape_rejected(self):
        for target in ("../escape", "../../escape", "/tmp/escape"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                archive = self.make_archive(directory, "source/link", target)
                with self.assertRaises((RuntimeError, tarfile.FilterError)):
                    lab.extract_archive(archive, directory / "source", "source")
                self.assertFalse((directory / "source").exists())

    def test_explicit_unused_member_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = self.make_archive(directory, "source/link", "/usr/include")
            with tarfile.open(archive, "a") as bundle:
                member = tarfile.TarInfo("source/file")
                member.size = 4
                bundle.addfile(member, io.BytesIO(b"data"))
            lab.extract_archive(archive, directory / "source", "source", ["source/link"])
            self.assertFalse((directory / "source/link").is_symlink())
            self.assertEqual((directory / "source/file").read_bytes(), b"data")


class BootEvidenceTests(unittest.TestCase):
    def test_success_requires_marker_and_shutdown(self):
        lab.validate_console("AXI_LAB_BOOT_PASS\r\n[1.00] reboot: Power down\r\n")
        for console in ("", "AXI_LAB_BOOT_PASS\n", "reboot: Power down\n",
                        "echo AXI_LAB_BOOT_PASS\nreboot: Power down\n",
                        "AXI_LAB_BOOT_FAIL\nAXI_LAB_BOOT_PASS\nreboot: Power down\n",
                        "FAIL\nAXI_LAB_BOOT_PASS\nreboot: Power down\n",
                        "can't open /dev/null\nAXI_LAB_BOOT_PASS\nreboot: Power down\n",
                        "AXI_LAB_BOOT_PASS\nKernel panic\nreboot: Power down\n"):
            with self.subTest(console=console), self.assertRaises(RuntimeError):
                lab.validate_console(console)

    def test_failed_command_is_not_success(self):
        with tempfile.TemporaryDirectory(dir=lab.ROOT) as temporary:
            with self.assertRaisesRegex(RuntimeError, "exited 7"):
                lab.logged([sys.executable, "-c", "raise SystemExit(7)"], Path(temporary) / "fail.log")

    def test_timeout_terminates_process(self):
        with tempfile.TemporaryDirectory(dir=lab.ROOT) as temporary:
            with self.assertRaises(subprocess.TimeoutExpired):
                lab.logged([sys.executable, "-c", "import time; time.sleep(60)"],
                           Path(temporary) / "timeout.log", timeout=0.1)


if __name__ == "__main__":
    unittest.main()
