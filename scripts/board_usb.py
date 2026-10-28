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
import tarfile
import tempfile
import time

import framefx


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "linux/hardware/ultra96-v1/usb_check.py"
RENDERER = ROOT / "linux/hardware/ultra96-mikro/display.c"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_route(route, interface):
    if len(route) != 1 or route[0].get("dev") != interface or route[0].get("gateway"):
        raise RuntimeError("Board address must route directly through the specified USB interface")
    return str(ipaddress.IPv4Address(route[0]["prefsrc"]))


def validate_usb(interface, sysfs=Path("/sys/class/net")):
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", interface):
        raise ValueError("Invalid interface name")
    device = (sysfs / interface / "device").resolve(strict=True)
    for parent in (device, *device.parents):
        if (parent / "idVendor").is_file() and (parent / "idProduct").is_file():
            identity = ((parent / "idVendor").read_text().strip(), (parent / "idProduct").read_text().strip())
            if identity not in (("03fd", "010c"), ("03fd", "010d")):
                raise RuntimeError("USB device is not the reviewed PYNQ gadget")
            return ":".join(identity)
    raise RuntimeError("Interface has no USB device identity")


def reference_cases():
    generator = random.Random(9601)
    cases = []
    for width, height in ((1, 1), (2, 2), (3, 3), (7, 5), (65, 9), (320, 240), (1024, 1024)):
        for mode in range(4):
            if mode == 3 and min(width, height) < 3:
                continue
            for threshold in (0, 127, 255):
                seed = generator.randrange(2**32)
                source = framefx.image(width, height, 0, seed, threshold)
                output = framefx.transform(source, width, height, mode, threshold)
                cases.append(dict(width=width, height=height, mode=mode, threshold=threshold, seed=seed,
                                  sha256=hashlib.sha256(output).hexdigest()))
    return cases


def sources():
    return {str(path.relative_to(ROOT)): sha256(path) for path in
            (Path(__file__).resolve(), ROOT / "scripts/framefx.py", CHECKER, RENDERER)}


def evidence_current(record):
    try:
        directory = (ROOT / record["directory"]).resolve()
        if not directory.is_relative_to((ROOT / "artifacts/hardware").resolve()):
            return False
        artifacts = record["artifact_sha256"]
        required = {"cases.json", "report.stdout", "board-frame.ppm", "host-frame.ppm",
                    "transfer.bin", "download.stdout", "renderer.stdout"}
        return (record["status"] == "PASS" and record["stage"] == "H0-usb-userspace"
                and record["source_sha256"] == sources() and set(artifacts) == required
                and all(sha256(directory / name) == value for name, value in artifacts.items())
                and json.loads((directory / "report.stdout").read_text()) == record["native"]
                and record["native"]["status"] == "PASS"
                and record["native"]["renderer_sha256"] == artifacts["renderer.stdout"]
                and record["native"]["cases_sha256"] == artifacts["cases.json"]
                and artifacts["board-frame.ppm"] == artifacts["host-frame.ppm"] == record["render_sha256"]
                and artifacts["transfer.bin"] == artifacts["download.stdout"] == record["usb_round_trip"]["sha256"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Explicit trusted-USB H0 userspace verification; no sudo, reflash or FPGA access")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--host", default="192.168.3.1")
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    arguments = parser.parse_args()
    address = str(ipaddress.IPv4Address(arguments.host))
    identity = validate_usb(arguments.interface)
    route = json.loads(subprocess.check_output(["ip", "-j", "route", "get", address], text=True))
    source_address = validate_route(route, arguments.interface)
    for path in (arguments.key, arguments.known_hosts):
        if not path.is_file() or path.is_symlink() or path.stat().st_uid != os.getuid():
            raise RuntimeError("SSH inputs must be owned regular files")
    if arguments.key.stat().st_mode & 0o077 or arguments.known_hosts.stat().st_mode & 0o022:
        raise RuntimeError("Unsafe SSH key/known-hosts permissions")
    ssh = ["ssh", "-F", "/dev/null", "-T", "-b", source_address, "-i", str(arguments.key.resolve()),
           "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "GlobalKnownHostsFile=/dev/null", "-o", f"UserKnownHostsFile={arguments.known_hosts.resolve()}",
           "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", "xilinx@" + address]
    os.umask(0o077)
    hardware = ROOT / "artifacts/hardware"
    hardware.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="usb-check-", dir=hardware))
    record = dict(schema=1, stage="H0-usb-userspace", status="FAIL", source_sha256=sources(),
                  time_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), usb_identity=identity,
                  interface=arguments.interface, host=address, physical_display="NOT_TESTED",
                  physical_framefx_dma_irq="NOT_TESTED", directory=str(directory.relative_to(ROOT)))

    def remote(command, name, data=None, timeout=60):
        result = subprocess.run([*ssh, shlex.join(command)], input=data, capture_output=True, timeout=timeout)
        (directory / (name + ".stdout")).write_bytes(result.stdout)
        (directory / (name + ".stderr")).write_bytes(result.stderr)
        if result.returncode:
            raise RuntimeError(f"{name} failed ({result.returncode}); see {directory}")
        return result.stdout

    try:
        compatible = remote(["cat", "/proc/device-tree/compatible"], "identity").split(b"\0")
        if b"avnet,ultra96-rev1" not in compatible:
            raise RuntimeError("Remote board is not Ultra96 V1")
        workspace = remote(["mktemp", "-d", "/home/xilinx/axi-usb-check.XXXXXX"], "workspace").decode().strip()
        if not re.fullmatch(r"/home/xilinx/axi-usb-check\.[A-Za-z0-9]+", workspace):
            raise RuntimeError("Unexpected remote scratch directory")
        record["remote_workspace"] = workspace
        (directory / "cases.json").write_text(json.dumps(reference_cases()) + "\n")
        (directory / "transfer.bin").write_bytes(random.Random(9602).randbytes(8 * 2**20))
        with tarfile.open(directory / "payload.tar", "w") as archive:
            for path, name in ((CHECKER, "usb_check.py"), (RENDERER, "display.c"),
                               (directory / "cases.json", "cases.json"), (directory / "transfer.bin", "transfer.bin")):
                archive.add(path, arcname=name)
        started = time.monotonic()
        remote(["tar", "-xf", "-", "-C", workspace], "upload", (directory / "payload.tar").read_bytes())
        returned = remote(["cat", workspace + "/transfer.bin"], "download")
        if hashlib.sha256(returned).hexdigest() != sha256(directory / "transfer.bin"):
            raise RuntimeError("USB SSH round-trip mismatch")
        record["usb_round_trip"] = dict(bytes=len(returned), sha256=sha256(directory / "transfer.bin"),
                                        seconds=time.monotonic() - started)
        remote(["gcc", "--version"], "compiler")
        remote(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", workspace + "/display.c",
                "-o", workspace + "/renderer"], "compile", timeout=90)
        remote(["timeout", "180", "python3", workspace + "/usb_check.py", "--renderer", workspace + "/renderer",
                "--cases", workspace + "/cases.json", "--output", workspace + "/report.json"], "native", timeout=195)
        native = json.loads(remote(["cat", workspace + "/report.json"], "report"))
        if native["status"] != "PASS" or native["cases_sha256"] != sha256(directory / "cases.json"):
            raise RuntimeError("Native evidence failed validation")
        record["native"] = native
        remote(["cat", workspace + "/renderer"], "renderer")
        remote([workspace + "/renderer", "--render", workspace + "/frame.ppm"], "render")
        rendered = remote(["cat", workspace + "/frame.ppm"], "frame")
        (directory / "board-frame.ppm").write_bytes(rendered)
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(RENDERER),
                        "-o", str(directory / "renderer-host")], check=True, timeout=60)
        subprocess.run([directory / "renderer-host", "--render", directory / "host-frame.ppm"], check=True, timeout=15)
        if hashlib.sha256(rendered).hexdigest() != sha256(directory / "host-frame.ppm"):
            raise RuntimeError("Native board render differs from host render")
        record["render_sha256"] = hashlib.sha256(rendered).hexdigest()
        record["artifact_sha256"] = {name: sha256(directory / name) for name in
                                     ("cases.json", "report.stdout", "board-frame.ppm", "host-frame.ppm",
                                      "transfer.bin", "download.stdout", "renderer.stdout")}
        if native["renderer_sha256"] != record["artifact_sha256"]["renderer.stdout"]:
            raise RuntimeError("Executed native binary changed during verification")
        if sources() != record["source_sha256"]:
            raise RuntimeError("Sources changed during verification")
        record["status"] = "PASS"
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        (directory / "manifest.json").write_text(json.dumps(record, indent=2) + "\n")
        (ROOT / "artifacts/latest-usb-check.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"H0_USB_PASS: {directory}; CPU/userspace only, physical display/PL/DMA/IRQ NOT TESTED")


if __name__ == "__main__":
    main()
