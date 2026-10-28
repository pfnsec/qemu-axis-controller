#!/usr/bin/env python3
import argparse
import hashlib
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def digest(data):
    return hashlib.sha256(data).hexdigest()


def memory_worker(cpu, size=32 * 2**20, passes=4):
    if not 2**20 <= size <= 32 * 2**20 or size % 65536 or not 1 <= passes <= 8:
        raise ValueError("Invalid bounded memory workload")
    os.sched_setaffinity(0, {cpu})
    started = time.monotonic()
    hashes = []
    with mmap.mmap(-1, size) as region:
        for iteration in range(passes):
            block = bytes((value ^ (iteration * 37 + cpu)) & 255 for value in range(256)) * 256
            expected = hashlib.sha256()
            for offset in range(0, size, len(block)):
                region[offset:offset + len(block)] = block
                expected.update(block)
            actual = digest(region)
            if actual != expected.hexdigest():
                raise RuntimeError("Anonymous-memory pattern mismatch")
            hashes.append(actual)
    return dict(cpu=cpu, affinity=sorted(os.sched_getaffinity(0)), bytes=size,
                passes=passes, sha256=hashes, seconds=time.monotonic() - started)


def storage_check(directory, size=16 * 2**20):
    if not 2**20 <= size <= 16 * 2**20 or size % 65536:
        raise ValueError("Invalid bounded file workload")
    block = bytes(range(256)) * 256
    expected = hashlib.sha256()
    started = time.monotonic()
    filename = None
    try:
        with tempfile.NamedTemporaryFile(prefix="storage-", dir=directory, delete=False) as output:
            filename = Path(output.name)
            for offset in range(0, size, len(block)):
                if output.write(block) != len(block):
                    raise RuntimeError("Short scratch-file write")
                expected.update(block)
            output.flush()
            os.fsync(output.fileno())
            os.posix_fadvise(output.fileno(), 0, size, os.POSIX_FADV_DONTNEED)
        with filename.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else None
            if actual is None:
                checksum = hashlib.sha256()
                for block in iter(lambda: source.read(65536), b""):
                    checksum.update(block)
                actual = checksum.hexdigest()
        if filename.stat().st_size != size or actual != expected.hexdigest():
            raise RuntimeError("Scratch-file readback mismatch")
        return dict(bytes=size, sha256=actual, seconds=time.monotonic() - started,
                    method="fsync, file-specific cache-eviction hint, reopen and hash; not a raw-media or power-loss test")
    finally:
        if filename is not None:
            filename.unlink(missing_ok=True)


def reference_check(renderer, cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 128:
        raise ValueError("Invalid case count")
    results = []
    for case in cases:
        width, height, mode, threshold, seed = (case[name] for name in ("width", "height", "mode", "threshold", "seed"))
        if (any(type(value) is not int for value in (width, height, mode, threshold, seed))
                or not 1 <= width <= 1024 or not 1 <= height <= 1024 or mode not in range(4)
                or not 0 <= threshold <= 255 or not 0 <= seed < 2**32
                or (mode == 3 and min(width, height) < 3)):
            raise ValueError("Invalid reference case")
        started = time.monotonic()
        result = subprocess.run([str(renderer), "--reference", *map(str, (width, height, mode, threshold, seed))],
                                capture_output=True, timeout=15, check=True)
        if len(result.stdout) != width * height or digest(result.stdout) != case["sha256"]:
            raise RuntimeError(f"Native reference mismatch: {width}x{height} mode {mode}")
        results.append(dict(**case, seconds=time.monotonic() - started))
    return results


def inventory():
    paths = {"model": "/proc/device-tree/model", "compatible": "/proc/device-tree/compatible",
             "boot_id": "/proc/sys/kernel/random/boot_id", "meminfo": "/proc/meminfo",
             "cmdline": "/proc/cmdline", "uptime": "/proc/uptime"}
    result = {name: Path(path).read_text().strip("\x00\n") for name, path in paths.items()}
    result.update(kernel=os.uname().release, architecture=os.uname().machine,
                  cpus=sorted(os.sched_getaffinity(0)), time_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    result["connectors"] = {str(path.parent.name): path.read_text().strip()
                            for path in Path("/sys/class/drm").glob("card*-*/status")}
    result["temperature_raw"] = {str(path): path.read_text().strip()
                                 for path in Path("/sys/bus/iio/devices").glob("*/in_temp*") if path.is_file()}
    return result


def main():
    parser = argparse.ArgumentParser(description="Bounded H0 CPU/file checks; no MMIO, PL, DMA or raw block-device access")
    parser.add_argument("--memory-worker", type=int)
    parser.add_argument("--renderer", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.memory_worker is not None:
        print(json.dumps(memory_worker(arguments.memory_worker)))
        return
    if not arguments.renderer or not arguments.cases or not arguments.output:
        parser.error("--renderer, --cases and --output are required")
    os.umask(0o077)
    report = dict(schema=1, stage="H0-native-userspace", status="FAIL", physical_display="NOT_TESTED",
                  physical_framefx_dma_irq="NOT_TESTED")
    workers = []
    try:
        report["before"] = inventory()
        if "avnet,ultra96-rev1" not in report["before"]["compatible"].split("\x00"):
            raise RuntimeError("This check requires the identified Ultra96 V1")
        available = int(next(line.split()[1] for line in report["before"]["meminfo"].splitlines()
                             if line.startswith("MemAvailable:")))
        if available < 512 * 1024:
            raise RuntimeError("Less than 512 MiB available; declining workload")
        report["renderer_sha256"] = digest(arguments.renderer.read_bytes())
        report["cases_sha256"] = digest(arguments.cases.read_bytes())
        report["reference"] = reference_check(arguments.renderer, json.loads(arguments.cases.read_text()))
        for cpu in report["before"]["cpus"][:4]:
            workers.append(subprocess.Popen([sys.executable, __file__, "--memory-worker", str(cpu)],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        report["memory"] = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=45)
            if worker.returncode:
                raise RuntimeError("Memory worker failed: " + stderr.decode(errors="replace")[-4096:])
            report["memory"].append(json.loads(stdout))
        report["storage"] = storage_check(arguments.output.parent)
        report["after"] = inventory()
        if report["before"]["boot_id"] != report["after"]["boot_id"]:
            raise RuntimeError("Boot identity changed during checks")
        report["status"] = "PASS"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate()
        arguments.output.write_text(json.dumps(report, indent=2) + "\n")
    print("H0_USB_NATIVE_PASS; CPU/file checks only, physical display/PL/DMA/IRQ NOT TESTED")


if __name__ == "__main__":
    main()
