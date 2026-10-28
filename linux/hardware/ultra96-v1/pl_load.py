import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile


CONFIRMATION = "LOAD_VOLATILE_FRAMEFX_ULTRA96_V1"
FILES = {"framefx.bit.bin", "framefx-afi.dtbo", "framefx.dtbo", "framefx.ko",
         "framefx-probe", "framefx-test", "platform.json"}
MANAGER = Path("/sys/class/fpga_manager/fpga0")
OVERLAYS = Path("/sys/kernel/config/device-tree/overlays")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked_bundle(directory):
    manifest = json.loads((directory / "load-manifest.json").read_text())
    if manifest.get("schema") != 1 or set(manifest.get("files", {})) != FILES:
        raise ValueError("Unexpected load manifest")
    payloads = {}
    for name in sorted(FILES):
        path = directory / name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 16 * 2**20:
            raise ValueError("Unsafe or oversized payload: " + name)
        data = path.read_bytes()
        if digest(data) != manifest["files"][name]:
            raise ValueError("Payload digest mismatch: " + name)
        payloads[name] = data
    settings = json.loads(payloads["platform.json"])
    if settings.get("part") != "xczu3eg-sbva484-1-e" or settings.get("kernel") != "5.15.19-xilinx-v2022.1" or \
            settings.get("control", {}).get("base") != 0x80000000 or settings.get("dma", {}).get("coherent") is not False:
        raise ValueError("Bundle is not the reviewed V1 platform")
    return payloads, settings


def check_interrupts(messages, hardware_irq):
    entries = re.findall(r"^\s*\d+:\s+(?:\d+\s+)+GICv[23]\s+(\d+)\s+(?:Level|Edge)\s+(.+)$",
                         messages, re.MULTILINE)
    if not entries:
        raise RuntimeError("GIC interrupt ownership is unavailable")
    for number, owner in entries:
        if int(number) == hardware_irq:
            raise RuntimeError("IRQ collision (GIC " + str(hardware_irq) + "): " + owner +
                               "; review ownership before programming; do not share or steal an active IRQ")


def preflight(settings):
    if os.geteuid() != 0:
        raise RuntimeError("Root is required for resource-map verification")
    if b"avnet,ultra96-rev1" not in Path("/proc/device-tree/compatible").read_bytes().split(b"\0"):
        raise RuntimeError("Wrong physical board")
    if platform.release() != settings["kernel"]:
        raise RuntimeError("Wrong board kernel")
    for name, key in (("BOOT.BIN", "boot_sha256"), ("image.ub", "image_ub_sha256")):
        if digest((Path("/boot") / name).read_bytes()) != settings[key]:
            raise RuntimeError("Boot baseline changed: " + name)
    if MANAGER.joinpath("name").read_text().strip() != "Xilinx ZynqMP FPGA Manager":
        raise RuntimeError("Unexpected FPGA manager")
    state = MANAGER.joinpath("state").read_text().strip()
    if state != "unknown":
        raise RuntimeError("Only an unconfigured fresh-boot PL is supported; no hot replacement")
    if not OVERLAYS.is_dir() or any(path.is_dir() for path in OVERLAYS.iterdir()):
        raise RuntimeError("Configfs overlay directory unavailable or already in use")
    if Path("/sys/module/framefx").exists() or Path("/dev/framefx0").exists():
        raise RuntimeError("Framefx already installed in this boot")
    region = Path("/proc/device-tree/fpga-full")
    if not region.is_dir() or any(path.is_dir() for path in region.iterdir()):
        raise RuntimeError("PL region is missing or already has child devices")
    if Path("/proc/device-tree/axi/smmu@fd800000/status").read_bytes().rstrip(b"\0") != b"disabled":
        raise RuntimeError("Unreviewed IOMMU configuration")
    memory = Path("/proc/meminfo").read_text()
    for field, minimum in (("MemAvailable", 512 * 1024), ("CmaFree", 16 * 1024)):
        match = re.search(r"^" + field + r":\s+(\d+) kB$", memory, re.MULTILINE)
        if not match or int(match[1]) < minimum:
            raise RuntimeError("Insufficient " + field)
    iomem = Path("/proc/iomem").read_text()
    resources = re.findall(r"^\s*([0-9a-f]+)-([0-9a-f]+) : (.+)$", iomem, re.MULTILINE)
    if not resources or not any(int(end, 16) for _, end, _ in resources):
        raise RuntimeError("Resource addresses are unavailable")
    for start, end, name in resources:
        if int(start, 16) < 0x80001000 and int(end, 16) >= 0x80000000:
            raise RuntimeError("MMIO collision: " + name)
    interrupts = Path("/proc/interrupts").read_text()
    check_interrupts(interrupts, settings["interrupt"]["gic_id"])
    messages = subprocess.check_output(["dmesg"], text=True, timeout=10)
    if re.search(r"rcu.*(?:stall|starved)|\bSError\b|\bKernel panic\b|\bOops:", messages, re.IGNORECASE):
        raise RuntimeError("Kernel is not healthy; recover before PL programming")
    return dict(boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                kernel=platform.release(), state=state, iomem=iomem, interrupts=interrupts)


def check_device(bound=False):
    device = Path("/sys/bus/platform/devices/80000000.framefx")
    node = Path("/proc/device-tree/fpga-full/framefx@80000000")
    if not device.is_dir() or not device.joinpath("of_node").is_symlink() or \
            device.joinpath("of_node").resolve() != node.resolve() or \
            node.joinpath("compatible").read_bytes() != b"example,axi-framefx-1.0\0":
        raise RuntimeError("Overlay did not create the expected framefx platform device; do not hot-reload")
    if bound and (not device.joinpath("driver").is_symlink() or \
                  device.joinpath("driver").resolve().name != "framefx" or \
                  not Path("/dev/framefx0").is_char_device()):
        raise RuntimeError("Framefx driver did not bind/create its character device; do not hot-reload")


def load(payloads, settings, confirmation):
    if confirmation != CONFIRMATION:
        raise ValueError("Explicit volatile-load confirmation is required")
    baseline = preflight(settings)
    directory = Path(tempfile.mkdtemp(prefix="axi-pl-", dir="/var/tmp"))
    os.chmod(directory, 0o700)
    report = dict(schema=1, status="FAIL", stage="preflight", baseline=baseline,
                  time_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  files={name: digest(data) for name, data in payloads.items()},
                  dma_tested=False, physical_display_tested=False)

    def save(stage):
        report["stage"] = stage
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(stage + " " + str(directory), flush=True)

    def run(command, name):
        completed = subprocess.run(command, capture_output=True, timeout=30)
        (directory / (name + ".stdout")).write_bytes(completed.stdout)
        (directory / (name + ".stderr")).write_bytes(completed.stderr)
        if completed.returncode:
            raise RuntimeError(name + " failed; retain PL and evidence, do not hot-reload")
        return completed.stdout.decode()

    try:
        for name, data in payloads.items():
            (directory / name).write_bytes(data)
            os.chmod(directory / name, 0o700 if name in ("framefx-probe", "framefx-test") else 0o600)
        vermagic = run(["modinfo", "-F", "vermagic", str(directory / "framefx.ko")], "vermagic")
        if vermagic.split()[0] != settings["kernel"]:
            raise RuntimeError("Module vermagic differs from running kernel")
        firmware_name = "axi-framefx-" + digest(payloads["framefx.bit.bin"]) + ".bin"
        firmware = Path("/lib/firmware") / firmware_name
        if firmware.exists() or firmware.is_symlink():
            if firmware.is_symlink() or not firmware.is_file() or firmware.stat().st_uid != 0 or \
                    firmware.stat().st_mode & 0o022 or firmware.read_bytes() != payloads["framefx.bit.bin"]:
                raise RuntimeError("Unsafe existing firmware file")
        else:
            with firmware.open("xb") as stream:
                stream.write(payloads["framefx.bit.bin"])
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(firmware, 0o600)
        save("programming")
        MANAGER.joinpath("flags").write_text("0")
        MANAGER.joinpath("firmware").write_text(firmware_name)
        state = MANAGER.joinpath("state").read_text().strip()
        if state != "operating":
            raise RuntimeError("FPGA manager did not report operating")
        report["manager_state"] = state
        save("programmed")
        afi = OVERLAYS / "axi-lab-afi"
        afi.mkdir()
        afi.joinpath("dtbo").write_bytes(payloads["framefx-afi.dtbo"])
        driver = Path("/sys/bus/platform/devices/axi-lab-afi/driver")
        if not driver.is_symlink() or driver.resolve().name != "afi-fpga":
            raise RuntimeError("AFI configuration driver did not bind")
        save("interfaces-configured")
        device = OVERLAYS / "axi-lab-framefx"
        device.mkdir()
        device.joinpath("dtbo").write_bytes(payloads["framefx.dtbo"])
        save("checking-device")
        check_device()
        save("binding-driver")
        run(["insmod", str(directory / "framefx.ko")], "insmod")
        check_device(bound=True)
        save("checking-CSR")
        output = run([str(directory / "framefx-probe")], "csr")
        if "FRAMEFX_CSR_PASS reads=64" not in output:
            raise RuntimeError("CSR verification marker missing")
        report["status"] = "PASS"
        save("H1-volatile-CSR-only")
        print(output, end="", flush=True)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return directory


def main():
    parser = argparse.ArgumentParser(description="Fresh-boot-only Ultra96 V1 PL loader; never writes boot media or starts DMA")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--confirm", default="")
    arguments = parser.parse_args()
    payloads, settings = checked_bundle(arguments.bundle)
    if not arguments.confirm:
        print(json.dumps(preflight(settings), indent=2))
        return
    with open("/run/axi-lab-pl.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        load(payloads, settings, arguments.confirm)


if __name__ == "__main__":
    main()
