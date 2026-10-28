import argparse
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import shlex
import subprocess
import tempfile

import board
import board_usb
import lab
import mikro


FILES = ("/proc/device-tree/compatible", "/proc/sys/kernel/osrelease", "/proc/sys/kernel/random/boot_id",
         "/proc/cmdline", "/proc/mounts", "/etc/axi-lab-release")
FATAL = r"rcu.*(?:stall|starved)|\bSError\b|\bKernel panic\b|\bOops:|\bBUG:|WARNING: CPU:|DMA-API:"


def sources():
    paths = (Path(__file__).resolve(), lab.ROOT / "scripts/board_usb.py", lab.ROOT / "scripts/framefx.py",
             lab.ROOT / "scripts/mikro.py", lab.ROOT / "fpga/ultra96-v1/boot-image.lock.json")
    return {str(path.relative_to(lab.ROOT)): lab.sha256(path) for path in paths}


def validate_candidate(candidate):
    if (candidate.get("status") != "PASS" or candidate.get("stage") != "H0-mikro-candidate"
            or candidate.get("kernel") != json.loads(board.LOCK_FILE.read_text())["kernel"]
            or candidate.get("image_bytes") != 271581184
            or candidate.get("recipe_sha256") != mikro.recipe_id(candidate.get("source_sha256", {}), candidate.get("pl_inputs"))
            or bool(candidate.get("framefx_pl_included")) != bool(candidate.get("pl_inputs"))
            or not candidate.get("source_sha256")):
        raise ValueError("Expected a verified frozen mikro build manifest")
    for field in ("image_sha256", "ramdisk_sha256", "renderer_sha256", "reference_ppm_sha256", "recipe_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", candidate.get(field, "")):
            raise ValueError("Invalid candidate digest: " + field)
    for field in ("image", "ramdisk"):
        filename = board.inside(lab.ROOT, candidate[field])
        if (not filename.resolve().is_relative_to(lab.ARTIFACTS.resolve()) or filename.is_symlink()
                or not filename.is_file() or lab.sha256(filename) != candidate[field + "_sha256"]):
            raise ValueError("Frozen candidate artifact changed: " + field)
    if board.inside(lab.ROOT, candidate["image"]).stat().st_size != candidate["image_bytes"]:
        raise ValueError("Frozen image length changed")


def validate_snapshot(snapshot, candidate):
    if "avnet,ultra96-rev1" not in snapshot["/proc/device-tree/compatible"].split("\0"):
        raise RuntimeError("Not an Ultra96 V1")
    if snapshot["/proc/sys/kernel/osrelease"].strip() != candidate["kernel"]:
        raise RuntimeError("Running kernel differs from the frozen candidate")
    boot_id = snapshot["/proc/sys/kernel/random/boot_id"].strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id):
        raise RuntimeError("Invalid boot identity")
    release = "AXI_LAB_MIKRO_RECIPE_SHA256=" + candidate["recipe_sha256"]
    if snapshot["/etc/axi-lab-release"].splitlines() != [release]:
        raise RuntimeError("Running recipe differs from the frozen candidate")
    command = shlex.split(snapshot["/proc/cmdline"])
    if ([value for value in command if value.startswith("root=")] != ["root=/dev/mmcblk0p2"]
            or "rootfstype=ext4" not in command or any(value.startswith("axi_lab.mikro_smoke=") for value in command)):
        raise RuntimeError("Expected physical SD boot, not initramfs smoke")
    roots = [line.split() for line in snapshot["/proc/mounts"].splitlines()
             if len(line.split()) == 6 and line.split()[1] == "/"]
    if (len(roots) != 1 or roots[0][0] not in ("/dev/root", "/dev/mmcblk0p2")
            or roots[0][2] != "ext4" or "rw" not in roots[0][3].split(",")):
        raise RuntimeError("Expected a writable SD ext4 root")
    if snapshot["renderer.sha256"].split() != [candidate["renderer_sha256"], "/usr/bin/axi-lab-display"]:
        raise RuntimeError("Running renderer differs from the frozen candidate")
    if snapshot["key.sha256"].split() != [candidate["ssh"]["public_key_sha256"], "/root/.ssh/authorized_keys"]:
        raise RuntimeError("Authorized client identity differs from the frozen candidate")
    if snapshot["private-modes"].splitlines() != ["0:0:600", "0:0:600"]:
        raise RuntimeError("Runtime credentials must be root-owned and private")
    if re.search(FATAL, snapshot["dmesg"], re.IGNORECASE):
        raise RuntimeError("Kernel fault detected; preserve evidence before further board work")
    return boot_id


def validate_services(output, candidate=None):
    marker = "AXI_LAB_MIKRO_SERVICES_PASS wifi=CONFIG_ONLY modules=HASH_ONLY tls=BUILD_ONLY"
    if marker not in output.splitlines():
        raise RuntimeError("Userspace service checks did not finish")
    if (candidate and candidate.get("framefx_pl_included")
            and "AXI_LAB_MIKRO_PL_BUNDLE_PASS programming=NOT_ATTEMPTED" not in output.splitlines()):
        raise RuntimeError("Packaged PL checks did not finish; programming is not part of this test")


class Connection:
    def __init__(self, interface, host, key, known_hosts, directory):
        self.identity = board_usb.validate_usb(interface)
        if self.identity != "03fd:010d":
            raise RuntimeError("Expected the mikro ECM/ACM USB gadget")
        self.interface = interface
        self.host = str(ipaddress.IPv4Address(host))
        self.source = self.check_route()
        for filename in (key, known_hosts):
            if (filename.is_symlink() or not filename.is_file() or filename.stat().st_uid != os.getuid()
                    or filename.stat().st_mode & (0o077 if filename == key else 0o022)):
                raise ValueError("SSH key/known-hosts must be owned, regular and suitably private")
        self.command = ["ssh", "-F", "/dev/null", "-T", "-b", self.source, "-i", str(key.resolve()),
                        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
                        "-o", "GlobalKnownHostsFile=/dev/null", "-o", f"UserKnownHostsFile={known_hosts.resolve()}",
                        "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                        "root@" + self.host]
        self.directory = directory

    def check_route(self):
        if board_usb.validate_usb(self.interface) != self.identity:
            raise RuntimeError("USB device identity changed")
        route = json.loads(subprocess.check_output(["ip", "-j", "route", "get", self.host], text=True, timeout=10))
        return board_usb.validate_route(route, self.interface)

    def run(self, arguments, name, data=None, timeout=30):
        if self.check_route() != self.source:
            raise RuntimeError("USB source/route changed")
        result = subprocess.run([*self.command, shlex.join(map(str, arguments))], input=data,
                                capture_output=True, timeout=timeout)
        (self.directory / (name + ".stdout")).write_bytes(result.stdout)
        (self.directory / (name + ".stderr")).write_bytes(result.stderr)
        if result.returncode:
            raise RuntimeError(f"{name} failed ({result.returncode}); see {self.directory}")
        return result.stdout

    def snapshot(self, prefix):
        snapshot = {}
        for index, filename in enumerate(FILES):
            data = self.run(["head", "-c", "262145", filename], f"{prefix}-file-{index}")
            if len(data) > 262144:
                raise RuntimeError("Oversized board metadata")
            snapshot[filename] = data.decode()
        for name, command in (("renderer.sha256", ["sha256sum", "/usr/bin/axi-lab-display"]),
                              ("key.sha256", ["sha256sum", "/root/.ssh/authorized_keys"]),
                              ("private-modes", ["stat", "-c", "%u:%g:%a", "/etc/wpa_supplicant-lab.conf",
                                                  "/root/.ssh/authorized_keys"]), ("dmesg", ["dmesg"])):
            snapshot[name] = self.run(command, prefix + "-" + name).decode()
        lab.write_json(self.directory / (prefix + ".json"), snapshot)
        return snapshot


def verify(connection, candidate, directory):
    before = connection.snapshot("before")
    boot_id = validate_snapshot(before, candidate)
    services = connection.run(["/usr/sbin/axi-lab-check-userspace"], "services", timeout=60).decode()
    validate_services(services, candidate)
    workspace = connection.run(["mktemp", "-d", "/tmp/axi-mikro-check.XXXXXX"], "workspace").decode().strip()
    if not re.fullmatch(r"/tmp/axi-mikro-check\.[A-Za-z0-9]+", workspace):
        raise RuntimeError("Unsafe remote scratch directory")
    payload = random.Random(9602).randbytes(8 * 2**20)
    transfer_hash = hashlib.sha256(payload).hexdigest()
    connection.run(["sh", "-c", "umask 077; cat > " + shlex.quote(workspace + "/transfer.bin")], "upload", payload)
    returned = connection.run(["cat", workspace + "/transfer.bin"], "download")
    if returned != payload:
        raise RuntimeError("USB SSH round-trip mismatch")
    cases = board_usb.reference_cases()
    for index, case in enumerate(cases):
        pixels = connection.run(["/usr/bin/axi-lab-display", "--reference", *[case[key] for key in
                                 ("width", "height", "mode", "threshold", "seed")]], f"pixels-{index}")
        if len(pixels) != case["width"] * case["height"] or hashlib.sha256(pixels).hexdigest() != case["sha256"]:
            raise RuntimeError("Independent pixel comparison failed: " + str(index))
    lab.write_json(directory / "cases.json", cases)
    connection.run(["/usr/bin/axi-lab-display", "--render", workspace + "/frame.ppm"], "render")
    rendered = connection.run(["cat", workspace + "/frame.ppm"], "frame")
    if hashlib.sha256(rendered).hexdigest() != candidate["reference_ppm_sha256"]:
        raise RuntimeError("Board render differs from frozen build reference")
    after = connection.snapshot("after")
    if validate_snapshot(after, candidate) != boot_id:
        raise RuntimeError("Board rebooted during verification")
    connection.run(["rm", workspace + "/transfer.bin", workspace + "/frame.ppm"], "cleanup-files")
    connection.run(["rmdir", workspace], "cleanup-directory")
    return dict(boot_id=boot_id, kernel=candidate["kernel"], reference_cases=len(cases),
                round_trip_bytes=len(payload), round_trip_sha256=transfer_hash,
                framebuffer_accessed=False, framefx_accessed=False, sd_root="ext4-rw",
                wifi_association="NOT_TESTED", physical_display="NOT_TESTED", physical_dma_irq="NOT_TESTED")


def evidence_current(record):
    try:
        directory = board.inside(lab.ROOT, record["directory"])
        if not directory.resolve().is_relative_to((lab.ARTIFACTS / "hardware").resolve()):
            return False
        hashes = record["artifact_sha256"]
        required = {"candidate.json", "before.json", "after.json", "cases.json", "services.stdout", "frame.stdout",
                    "download.stdout", *[f"pixels-{index}.stdout" for index in range(78)]}
        candidate = json.loads((directory / "candidate.json").read_text())
        validate_candidate(candidate)
        validate_services((directory / "services.stdout").read_text(), candidate)
        cases = board_usb.reference_cases()
        if json.loads((directory / "cases.json").read_text()) != cases:
            return False
        for index, case in enumerate(cases):
            pixels = directory / f"pixels-{index}.stdout"
            if pixels.stat().st_size != case["width"] * case["height"] or lab.sha256(pixels) != case["sha256"]:
                return False
        transfer = directory / "download.stdout"
        transfer_hash = hashlib.sha256(random.Random(9602).randbytes(8 * 2**20)).hexdigest()
        return (record["status"] == "PASS" and record["stage"] == "H0-mikro-usb"
                and record["source_sha256"] == sources() and required.issubset(hashes)
                and all(Path(name).name == name and not (directory / name).is_symlink()
                        and lab.sha256(directory / name) == digest for name, digest in hashes.items())
                and record["image_sha256"] == candidate["image_sha256"]
                and record["boot"]["reference_cases"] == len(cases)
                and record["boot"]["round_trip_bytes"] == transfer.stat().st_size == 8 * 2**20
                and record["boot"]["round_trip_sha256"] == lab.sha256(transfer) == transfer_hash
                and validate_snapshot(json.loads((directory / "before.json").read_text()), candidate)
                    == validate_snapshot(json.loads((directory / "after.json").read_text()), candidate) == record["boot"]["boot_id"]
                and lab.sha256(directory / "frame.stdout") == candidate["reference_ppm_sha256"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Mikro USB/SD-boot userspace verification; no target Python, PL or framebuffer access")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--host", default="192.168.3.1")
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    arguments = parser.parse_args()
    candidate = json.loads(arguments.candidate.read_text())
    validate_candidate(candidate)
    os.umask(0o077)
    directory = Path(tempfile.mkdtemp(prefix="mikro-usb-", dir=lab.ARTIFACTS / "hardware"))
    lab.write_json(directory / "candidate.json", candidate)
    record = dict(schema=1, stage="H0-mikro-usb", status="FAIL", source_sha256=sources(),
                  time_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), image_sha256=candidate["image_sha256"],
                  directory=str(directory.relative_to(lab.ROOT)), interface=arguments.interface,
                  candidate_inputs_current=mikro.evidence_current(candidate), historical_observation_only=True)
    try:
        connection = Connection(arguments.interface, arguments.host, arguments.key, arguments.known_hosts, directory)
        record["usb_identity"] = connection.identity
        record["boot"] = verify(connection, candidate, directory)
        record["artifact_sha256"] = {filename.name: lab.sha256(filename) for filename in sorted(directory.iterdir())
                                     if filename.is_file()}
        record["status"] = "PASS"
        if not evidence_current(record):
            record["status"] = "FAIL"
            raise RuntimeError("Verification evidence changed during execution")
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        lab.write_json(directory / "manifest.json", record)
        lab.write_json(lab.ARTIFACTS / "latest-mikro-usb.json", record)
    print(f"MIKRO_USB_PASS: {directory}; display, Wi-Fi association and PL/DMA/IRQ NOT TESTED")


if __name__ == "__main__":
    main()
