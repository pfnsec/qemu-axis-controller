import datetime
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

import lab
import sdcard


LOCK_FILE = lab.ROOT / "fpga/ultra96-v1/boot-image.lock.json"
PAYLOAD = lab.ROOT / "linux/hardware/ultra96-v1"


def image_inputs():
    paths = [LOCK_FILE, lab.ROOT / "Makefile", lab.ROOT / "scripts/board.py", lab.ROOT / "scripts/lab.py", lab.ROOT / "scripts/sdcard.py"]
    paths.extend(path for path in PAYLOAD.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    return {str(path.relative_to(lab.ROOT)): lab.sha256(path) for path in paths}


def fetch_image():
    lock = json.loads(LOCK_FILE.read_text())
    downloads = lab.ARTIFACTS / "downloads/ultra96-v1"
    downloads.mkdir(parents=True, exist_ok=True)
    (lab.ARTIFACTS / "logs").mkdir(parents=True, exist_ok=True)
    archive = downloads / lock["archive_url"].rsplit("/", 1)[1]
    if not archive.exists():
        partial = archive.with_suffix(".7z.part")
        lab.logged(["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "30", "--max-time", "1800",
                    "--output", partial, lock["archive_url"]], lab.ARTIFACTS / "logs/ultra96-image-download.log", timeout=1900)
        lab.verify_archive(partial, lock["archive_sha256"])
        partial.replace(archive)
    lab.verify_archive(archive, lock["archive_sha256"])
    if archive.stat().st_size != lock["archive_bytes"]:
        raise RuntimeError("Incorrect vendor archive size")
    directory = lab.ARTIFACTS / "build/ultra96-v1/base"
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / lock["image_name"]
    if not image.exists():
        partial = image.with_suffix(".img.part")
        with partial.open("wb") as output, (lab.ARTIFACTS / "logs/ultra96-image-extract.log").open("w") as log:
            subprocess.run(["7z", "e", "-so", str(archive), lock["image_name"]], stdout=output, stderr=log, check=True, timeout=900)
        lab.verify_archive(partial, lock["image_sha256"])
        partial.replace(image)
    if image.stat().st_size != lock["image_bytes"]:
        raise RuntimeError("Incorrect vendor image size")
    lab.verify_archive(image, lock["image_sha256"])
    return image, lock


def validate_partitions(table, lock):
    if table.get("label") != "dos" or table.get("unit") != "sectors" or table.get("sectorsize") != 512:
        raise RuntimeError("Unexpected vendor partition-table format")
    actual = [{key: entry.get(key) for key in ("start", "size", "type")} for entry in table.get("partitions", [])]
    if actual != lock["partitions"]:
        raise RuntimeError("Vendor partition layout differs from pinned image")


def inside(root, relative, privileged=False):
    target = root / relative
    resolved = (Path(subprocess.check_output(["sudo", "-n", "realpath", "-m", str(target)], text=True).strip())
                if privileged else target.resolve())
    if not resolved.is_relative_to(root.resolve()):
        raise RuntimeError(f"Image path escapes mount: {relative}")
    return target


def prepare_desktop_home(root):
    passwd = inside(root, "etc/passwd", privileged=True)
    entries = [line.split(":") for line in subprocess.check_output(
        ["sudo", "-n", "cat", str(passwd)], text=True).splitlines() if line.startswith("xilinx:")]
    if (len(entries) != 1 or len(entries[0]) != 7 or entries[0][2:4] != ["1000", "1000"]
            or entries[0][5] != "/home/xilinx"):
        raise RuntimeError("Vendor desktop account differs from the pinned baseline")
    home = inside(root, "home/xilinx", privileged=True)
    command = ["sudo", "-n", "stat", "-c", "%f:%u:%g", str(home)]
    original = subprocess.check_output(command, text=True).strip()
    directory_mode = f"{stat.S_IFDIR | 0o750:x}"
    expected = directory_mode + ":1000:1000"
    if original not in (directory_mode + ":127:135", expected):
        raise RuntimeError("Vendor desktop home metadata differs from the pinned baseline")
    lab.run(["sudo", "-n", "chown", "--no-dereference", "1000:1000", home])
    actual = subprocess.check_output(command, text=True).strip()
    if actual != expected:
        raise RuntimeError("Desktop home ownership repair did not verify")
    return dict(path="/home/xilinx", before=original, after=actual)


def install_payload(root):
    installed = {}
    for source, relative in (("board_probe.py", "opt/axi-lab/board_probe.py"),
                             ("display.py", "opt/axi-lab/display.py"),
                             ("axi-lab-h0.service", "etc/systemd/system/axi-lab-h0.service"),
                             ("reference.js", "home/xilinx/axi-lab-reference.js"),
                             ("welcome.html", "home/xilinx/Welcome to Pynq.html")):
        target = inside(root, relative, privileged=True)
        lab.run(["sudo", "-n", "install", "-D", "-m", "0644", PAYLOAD / source, target])
        digest = subprocess.check_output(["sudo", "-n", "sha256sum", str(target)], text=True).split()[0]
        if digest != lab.sha256(PAYLOAD / source):
            raise RuntimeError(f"Installed payload digest mismatch: {relative}")
        installed[relative] = digest
    directory = inside(root, "etc/systemd/system/multi-user.target.wants", privileged=True)
    lab.run(["sudo", "-n", "ln", "-s", "../axi-lab-h0.service", directory / "axi-lab-h0.service"])
    startup = inside(root, "root/.fluxbox/startup", privileged=True)
    original = subprocess.check_output(["sudo", "-n", "cat", str(startup)], text=True)
    command = 'runuser -w DISPLAY -l xilinx -c " xdg-open file:///home/xilinx/Welcome%20to%20Pynq.html" &'
    if original.count(command) != 1:
        raise RuntimeError("Vendor desktop startup differs from the inspected baseline")
    replacement = 'runuser -u xilinx -- /bin/sh -c "/usr/bin/python3 /opt/axi-lab/display.py >> /home/xilinx/axi-lab-display.log 2>&1" &'
    with tempfile.NamedTemporaryFile(mode="w") as temporary:
        temporary.write(original.replace(command, replacement))
        temporary.flush()
        lab.run(["sudo", "-n", "cp", startup, inside(root, "root/.fluxbox/startup.vendor", privileged=True)])
        lab.run(["sudo", "-n", "install", "-m", "0755", temporary.name, startup])
        digest = subprocess.check_output(["sudo", "-n", "sha256sum", str(startup)], text=True).split()[0]
        if digest != lab.sha256(temporary.name):
            raise RuntimeError("Desktop startup installation digest mismatch")
        installed["root/.fluxbox/startup"] = digest
    return installed


def evidence_current(record):
    try:
        image = inside(lab.ROOT, record["image"])
        return (record["status"] == "PASS" and record["stage"] == "H0-image"
                and record["source_sha256"] == image_inputs()
                and image.stat().st_size == record["image_bytes"]
                and lab.sha256(image) == record["image_sha256"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def build_image():
    for tool in ("7z", "sudo", "losetup", "sfdisk", "mount", "umount", "install", "e2fsck"):
        if not shutil.which(tool):
            raise RuntimeError(f"board-image requires {tool}")
    base, lock = fetch_image()
    directory = lab.ARTIFACTS / "hardware" / datetime.datetime.now(datetime.timezone.utc).strftime("h0-%Y%m%dT%H%M%S-%fZ")
    directory.mkdir(parents=True)
    output = directory / "axi-lab-ultra96v1-h0.img"
    record = dict(schema=1, status="FAIL", stage="H0-image", time_utc=lab.timestamp(), hardware_tested=False,
                  display_tested=False, framefx_pl_included=False, directory=str(directory.relative_to(lab.ROOT)),
                  upstream=lock, source_sha256=image_inputs())
    try:
        lab.logged(["cp", "--reflink=auto", "--sparse=always", base, output], directory / "copy.log", timeout=300)
        table = json.loads(subprocess.check_output(["sfdisk", "--json", str(output)], text=True))["partitiontable"]
        validate_partitions(table, lock)
        record["partition_table"] = table
        boot_bytes = lock["partitions"][1]["start"] * 512
        with base.open("rb") as source:
            boot_hash = sdcard.digest_prefix(source, boot_bytes)
        with tempfile.TemporaryDirectory(prefix="ultra96-image-") as temporary:
            root = Path(temporary)
            loopdev = subprocess.check_output(["sudo", "-n", "losetup", "--find", "--show", "--partscan", str(output)], text=True).strip()
            mounted = False
            try:
                lab.run(["sudo", "-n", "mount", "-o", "rw,nosuid,nodev,noexec", loopdev + "p2", root])
                mounted = True
                record["desktop_home"] = prepare_desktop_home(root)
                record["installed_sha256"] = install_payload(root)
                lab.run(["sudo", "-n", "umount", root])
                mounted = False
                lab.logged(["sudo", "-n", "e2fsck", "-fn", loopdev + "p2"], directory / "rootfs-check.log", timeout=180)
            finally:
                try:
                    if mounted:
                        lab.run(["sudo", "-n", "umount", root])
                finally:
                    lab.run(["sudo", "-n", "losetup", "--detach", loopdev])
        if output.stat().st_size != lock["image_bytes"] or image_inputs() != record["source_sha256"]:
            raise RuntimeError("Image size or source inputs changed during packaging")
        with output.open("rb") as source:
            if sdcard.digest_prefix(source, boot_bytes) != boot_hash:
                raise RuntimeError("Vendor partition table or boot partition changed")
        record["preserved_boot_region"] = dict(bytes=boot_bytes, sha256=boot_hash)
        record.update(status="PASS", image=str(output.relative_to(lab.ROOT)), image_bytes=output.stat().st_size,
                      image_sha256=lab.sha256(output))
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        lab.write_json(directory / "manifest.json", record)
        lab.write_json(lab.ARTIFACTS / "latest-board-image.json", record)
    print(f"H0_IMAGE_PASS: {output.relative_to(lab.ROOT)}; SD write and physical boot NOT YET TESTED", flush=True)
