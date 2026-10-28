#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import time

import lab


def validate_target(devices, target, image_bytes):
    matches = [device for device in devices if device.get("path") == target]
    if len(matches) != 1:
        raise ValueError("Target must be an existing whole disk, not a partition")
    device = matches[0]
    if device.get("type") != "disk" or device.get("rm") is not True or device.get("ro") is not False or device.get("tran") != "usb":
        raise ValueError("Refusing target: require a writable removable USB disk")
    if type(device.get("size")) is not int or not 0 < image_bytes <= device["size"] <= 256 * 2**30:
        raise ValueError("Image does not fit, or target exceeds the 256-GiB safety limit")
    pending = [device]
    while pending:
        current = pending.pop()
        if any(current.get("mountpoints") or []):
            raise ValueError("Target or a descendant is mounted/in use; unmount explicitly first")
        if current is not device and current.get("type") != "part":
            raise ValueError("Refusing a disk with mapped/RAID/encrypted descendants")
        pending.extend(current.get("children", []))
    if not device.get("model") or not device.get("serial"):
        raise ValueError("Reader model/serial missing; cannot establish the required target identity")
    return {key: device[key] for key in ("path", "maj:min", "size", "model", "serial", "tran", "rm", "ro", "type")}


def identity_token(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def inspect(target, image_bytes):
    resolved = str(Path(target).resolve(strict=True))
    metadata = os.stat(resolved)
    if not stat.S_ISBLK(metadata.st_mode):
        raise ValueError("Target is not a block device")
    result = subprocess.run(["lsblk", "--json", "--bytes", "-o", "PATH,MAJ:MIN,RM,SIZE,RO,TYPE,TRAN,MODEL,SERIAL,MOUNTPOINTS"],
                            check=True, capture_output=True, text=True, timeout=15)
    identity = validate_target(json.loads(result.stdout)["blockdevices"], resolved, image_bytes)
    if identity["maj:min"] != f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}":
        raise ValueError("Target identity changed during inspection")
    disk = (Path("/sys/dev/block") / identity["maj:min"]).resolve()
    for directory in [disk, *(entry.parent for entry in disk.glob("*/partition"))]:
        for filename in (directory / "holders").iterdir():
            raise ValueError(f"Target has a block-device holder: {filename}")
    return identity


def digest_prefix(stream, length):
    if type(length) is not int or length <= 0:
        raise ValueError("Verification length must be a positive integer")
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = stream.read(min(4 * 2**20, remaining))
        if not chunk:
            raise RuntimeError("Unexpected end of image/device during verification")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def copy_image(source, destination, length, expected_sha256):
    if type(length) is not int or length <= 0:
        raise ValueError("Copy length must be a positive integer")
    digest = hashlib.sha256()
    remaining = length
    copied = 0
    next_report = 256 * 2**20
    while remaining:
        chunk = source.read(min(4 * 2**20, remaining))
        if not chunk:
            raise RuntimeError("Image truncated during write")
        digest.update(chunk)
        pending = memoryview(chunk)
        while pending:
            written = destination.write(pending)
            if written is None or written <= 0:
                raise RuntimeError("Short/failed device write")
            pending = pending[written:]
        remaining -= len(chunk)
        copied += len(chunk)
        if copied >= next_report or not remaining:
            print(f"SD_WRITE {copied}/{length} bytes", flush=True)
            next_report += 256 * 2**20
    if source.read(1) or digest.hexdigest() != expected_sha256:
        raise RuntimeError("Source image changed while writing; card is NOT verified")


def open_device(path):
    descriptor = os.open(path, os.O_RDWR | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return os.fdopen(descriptor, "r+b", buffering=0)
    except Exception:
        os.close(descriptor)
        raise


def flash(image, target, expected_sha256, expected_identity, evidence):
    if os.geteuid() != 0:
        raise ValueError("Writing requires root; inspect is non-destructive and does not")
    metadata = image.stat()
    if not stat.S_ISREG(metadata.st_mode) or image.is_symlink() or metadata.st_size % 512:
        raise ValueError("Require a regular, sector-aligned image file, not a symlink")
    if evidence.suffix != ".json" or evidence.is_symlink() or evidence.resolve() == image.resolve():
        raise ValueError("Evidence must be a separate JSON file, not an image or symlink")
    if evidence.exists() and not evidence.is_file():
        raise ValueError("Evidence path is not a regular file")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    identity = inspect(target, metadata.st_size)
    if identity_token(identity) != expected_identity:
        raise ValueError("Target identity differs from the inspected/confirmed token")
    record = dict(schema=1, status="FAIL", time_utc=lab.timestamp(), image=str(image.resolve()),
                  image_bytes=metadata.st_size, expected_sha256=expected_sha256, target=identity,
                  backup="WAIVED_BY_OPERATOR", physical_boot="NOT_TESTED")
    started = time.monotonic()
    try:
        with image.open("rb") as source:
            if digest_prefix(source, metadata.st_size) != expected_sha256:
                raise ValueError("Source SHA256 mismatch; no device write performed")
            source.seek(0)
            if inspect(target, metadata.st_size) != identity:
                raise ValueError("Target changed before opening")
            with open_device(identity["path"]) as destination:
                observed = os.fstat(destination.fileno())
                actual_device = f"{os.major(observed.st_rdev)}:{os.minor(observed.st_rdev)}"
                capacity = struct.unpack("Q", fcntl.ioctl(destination.fileno(), 0x80081272, bytes(8)))[0]
                if not stat.S_ISBLK(observed.st_mode) or actual_device != identity["maj:min"] or capacity != identity["size"]:
                    raise ValueError("Opened device does not match inspected identity/capacity")
                copy_image(source, destination, metadata.st_size, expected_sha256)
                if capacity >= metadata.st_size + 2**20:
                    destination.seek(capacity - 2**20)
                    cleared = destination.write(bytes(2**20))
                    if cleared != 2**20:
                        raise RuntimeError("Failed to clear stale tail partition metadata")
                os.fsync(destination.fileno())
                fcntl.ioctl(destination.fileno(), 0x1261)
                destination.seek(0)
                print("SD_VERIFY reading the complete written image back from the card", flush=True)
                observed_hash = digest_prefix(destination, metadata.st_size)
                if observed_hash != expected_sha256:
                    raise RuntimeError("Card readback SHA256 mismatch")
                record.update(status="PASS", readback_sha256=observed_hash)
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        with tempfile.NamedTemporaryFile(mode="w", dir=evidence.parent, prefix=".sd-evidence-", delete=False) as temporary:
            json.dump(record, temporary, indent=2)
            temporary.write("\n")
        Path(temporary.name).replace(evidence)
    print("SD_WRITE_VERIFY_PASS; physical boot/display are still unverified", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Guarded removable-USB SD writer; never selects a target automatically")
    parser.add_argument("action", choices=("inspect", "write"))
    parser.add_argument("--device", required=True)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--expect-identity")
    parser.add_argument("--erase-without-backup", action="store_true")
    parser.add_argument("--evidence", type=Path)
    arguments = parser.parse_args()
    if arguments.action == "inspect":
        identity = inspect(arguments.device, arguments.image.stat().st_size)
        print(json.dumps(dict(identity=identity, confirmation_token=identity_token(identity)), indent=2))
    else:
        if not arguments.erase_without_backup or not arguments.expect_identity or not arguments.sha256 or not arguments.evidence:
            parser.error("write requires --erase-without-backup, --expect-identity, --sha256 and --evidence")
        flash(arguments.image, arguments.device, arguments.sha256, arguments.expect_identity, arguments.evidence)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"SD_CARD_ERROR: {error}") from error
