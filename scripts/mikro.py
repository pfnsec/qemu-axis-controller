import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile

import board
import lab
import pl_bundle
import sdcard


PAYLOAD = lab.ROOT / "linux/hardware/ultra96-mikro"
BUILD = lab.ARTIFACTS / "build/ultra96-mikro"
ROOTFS_BYTES = 128 * 2**20
WIFI_MODULES = ("kernel/drivers/net/wireless/ti/wl18xx/wl18xx.ko",
                "kernel/drivers/net/wireless/ti/wlcore/wlcore_sdio.ko")


def private_config(filename):
    if filename is None:
        return None
    filename = lab.ROOT / filename
    if (filename.is_symlink() or not filename.resolve().is_relative_to(lab.ARTIFACTS.resolve())
            or not stat.S_ISREG(filename.stat().st_mode) or filename.stat().st_mode & 0o077
            or filename.stat().st_uid != os.getuid() or filename.stat().st_size > 65536):
        raise RuntimeError("Wi-Fi config must be an owned private regular file under ignored artifacts/ (0600)")
    return dict(path=str(filename.relative_to(lab.ROOT)), sha256=lab.sha256(filename))


def module_closure(text, requested):
    dependencies = {}
    for line in text.splitlines():
        name, separator, values = line.partition(":")
        if not separator or name in dependencies:
            raise ValueError("Malformed or duplicate vendor module dependency entry")
        paths = [name, *values.split()]
        if any(not path.startswith(("kernel/", "extra/")) or not path.endswith(".ko")
               or ".." in Path(path).parts or "\\" in path for path in paths):
            raise ValueError("Unsafe vendor module dependency path")
        dependencies[name] = paths[1:]
    ordered, active = [], set()

    def visit(name):
        if name in active or name not in dependencies:
            raise ValueError("Missing or cyclic vendor module dependency")
        if name in ordered:
            return
        active.add(name)
        for dependency in dependencies[name]:
            visit(dependency)
        active.remove(name)
        ordered.append(name)

    for name in requested:
        visit(name)
    return ordered


def inputs():
    paths = [board.LOCK_FILE, lab.ROOT / "versions.lock", lab.ROOT / "Makefile",
             lab.ROOT / "scripts/lab.py", lab.ROOT / "scripts/board.py", lab.ROOT / "scripts/sdcard.py",
             lab.ROOT / "scripts/mikro.py", lab.ROOT / "linux/buildroot/Config.in",
             lab.ROOT / "scripts/pl_bundle.py", lab.ROOT / "linux/hardware/ultra96-v1/pl_load.py",
             lab.ROOT / "linux/buildroot/external.mk", lab.ROOT / "linux/buildroot/configs/ultra96_mikro_defconfig"]
    for directory in (PAYLOAD, lab.ROOT / "linux/buildroot/package/axi-mikro-display"):
        paths.extend(path for path in directory.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    return {str(path.relative_to(lab.ROOT)): lab.sha256(path) for path in sorted(paths)}


def recipe_id(source, native_inputs=None):
    if native_inputs is not None:
        source = dict(source_sha256=source, pl_inputs=native_inputs)
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pl_inputs(filename):
    if filename is None:
        return None
    filename = lab.ROOT / filename
    if filename.is_symlink() or not filename.resolve().is_relative_to(lab.ARTIFACTS.resolve()):
        raise ValueError("PL native record must be a regular artifact under artifacts/")
    native = json.loads(filename.read_text())
    pl_bundle.validate_native(native, filename.parent)
    build_record = lab.ARTIFACTS / "ultra96-pl-bitstream.json"
    build = json.loads(build_record.read_text())
    if not pl_bundle.pl.evidence_current(build):
        raise ValueError("PL bitstream build is not source/artifact-current")
    return dict(native_record=str(filename.relative_to(lab.ROOT)), native_record_sha256=lab.sha256(filename),
                pl_build_record_sha256=lab.sha256(build_record))


def stage_pl(filename, overlay):
    bundle = pl_bundle.assemble(filename)
    target = overlay / "opt/axi-lab/pl"
    shutil.copytree(bundle, target)
    validate_pl_binaries(target)
    manifest = json.loads((target / "load-manifest.json").read_text())
    manifest["files"].update({"pl_load.py": manifest["loader_sha256"]})
    (overlay / "etc/axi-lab-pl.sha256").write_text("".join(
        f"{digest}  /opt/axi-lab/pl/{name}\n" for name, digest in sorted(manifest["files"].items())))
    for name in ("framefx-probe", "framefx-test"):
        (target / name).chmod(0o700)
    return dict(path="/opt/axi-lab/pl", file_sha256={name: lab.sha256(target / name) for name in
                (*manifest["files"], "load-manifest.json")}, automatic_load=False)


def validate_pl_binaries(directory):
    for name in ("framefx.ko", "framefx-probe", "framefx-test"):
        header = (directory / name).read_bytes()[:20]
        if (len(header) != 20 or header[:6] != b"\x7fELF\x02\x01"
                or int.from_bytes(header[18:20], "little") != 183
                or int.from_bytes(header[16:18], "little") not in ((1,) if name.endswith(".ko") else (2, 3))):
            raise ValueError("PL payload must be an AArch64 ELF: " + name)
    version = subprocess.check_output(["modinfo", "-F", "vermagic", str(directory / "framefx.ko")], text=True).split()
    if not version or version[0] != pl_bundle.pl.PLATFORM["kernel"]:
        raise ValueError("Framefx module does not match the preserved vendor kernel")


def evidence_current(record):
    try:
        image = board.inside(lab.ROOT, record["image"])
        ramdisk = board.inside(lab.ROOT, record["ramdisk"])
        return (record["status"] == "PASS" and record["stage"] == "H0-mikro-candidate"
                and record["source_sha256"] == inputs()
                and record["recipe_sha256"] == recipe_id(record["source_sha256"], record.get("pl_inputs"))
                and record.get("wifi_config") == private_config(record.get("wifi_config", {}).get("path")
                                                                if record.get("wifi_config") else None)
                and record.get("pl_inputs") == pl_inputs(record.get("pl_inputs", {}).get("native_record")
                                                        if record.get("pl_inputs") else None)
                and bool(record.get("framefx_pl_included")) == bool(record.get("pl_inputs"))
                and image.stat().st_size == record["image_bytes"]
                and lab.sha256(image) == record["image_sha256"]
                and lab.sha256(ramdisk) == record["ramdisk_sha256"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
        return False


def smoke_current(record, candidate):
    try:
        binaries = record["binary_sha256"]
        return (record["status"] == "PASS" and record["stage"] == "mikro-userspace-smoke"
                and record["source_sha256"] == inputs() and evidence_current(candidate)
                and record["image_sha256"] == candidate["image_sha256"] and bool(binaries)
                and all(lab.sha256(board.inside(lab.ROOT, name)) == digest for name, digest in binaries.items()))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
        return False


def ssh_key():
    directory = lab.ARTIFACTS / "hardware/keys"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    private = directory / "ultra96-mikro"
    if not private.exists():
        lab.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "axi-lab-ultra96-mikro", "-f", private])
    if private.is_symlink() or not stat.S_ISREG(private.stat().st_mode) or private.stat().st_mode & 0o077:
        raise RuntimeError("Mikro SSH private key must be a private regular file (mode 0600)")
    public = subprocess.check_output(["ssh-keygen", "-y", "-f", str(private)], text=True).strip() + "\n"
    if not public.startswith("ssh-ed25519 "):
        raise RuntimeError("Unexpected mikro SSH key format")
    return private, public


def kernel_contract(base, lock, directory, build=BUILD, include_pl=False):
    required = ("DEVTMPFS", "DEVTMPFS_MOUNT", "EXT4_FS", "DRM", "DRM_FBDEV_EMULATION",
                "DRM_ZYNQMP_DPSUB", "PHY_XILINX_ZYNQMP", "ZYNQMP_FIRMWARE", "ZYNQMP_PM_DOMAINS",
                "MMC_BLOCK", "MMC_SDHCI_OF_ARASAN", "UNIX", "INET", "UNIX98_PTYS",
                "FRAMEBUFFER_CONSOLE", "INPUT_EVDEV", "HID_GENERIC", "USB_HID", "USB_DWC3",
                "USB_DWC3_XILINX", "USB_DWC3_DUAL_ROLE", "USB_XHCI_HCD", "USB_XHCI_PLATFORM",
                "CONFIGFS_FS", "USB_CONFIGFS", "USB_CONFIGFS_ECM", "USB_CONFIGFS_ACM",
                "SERIAL_XILINX_PS_UART", "SERIAL_XILINX_PS_UART_CONSOLE", "MODULES", "CFG80211",
                "MAC80211", "RFKILL", "FW_LOADER")
    if include_pl:
        required += ("FPGA", "FPGA_REGION", "FPGA_MGR_ZYNQMP_FPGA", "XILINX_AFI_FPGA", "OF_CONFIGFS", "DMA_CMA")
    with tempfile.TemporaryDirectory(prefix="ultra96-mikro-contract-") as temporary:
        root = Path(temporary)
        loopdev = subprocess.check_output(["sudo", "-n", "losetup", "--find", "--show", "--read-only", "--partscan", str(base)], text=True).strip()
        mounted = False
        try:
            lab.run(["sudo", "-n", "mount", "-o", "ro,noload,nosuid,nodev,noexec", loopdev + "p2", root])
            mounted = True
            filename = board.inside(root, f"lib/modules/{lock['kernel']}/build/.config", privileged=True)
            configuration = subprocess.check_output(["sudo", "-n", "cat", str(filename)], text=True)
            vendor = stage_vendor_wifi(root, lock, build / "lab-overlay")
        finally:
            try:
                if mounted:
                    lab.run(["sudo", "-n", "umount", root])
            finally:
                lab.run(["sudo", "-n", "losetup", "--detach", loopdev])
    (directory / "vendor-kernel.config").write_text(configuration)
    for symbol in required:
        if f"CONFIG_{symbol}=y" not in configuration.splitlines():
            raise RuntimeError(f"Vendor kernel lacks built-in {symbol}; do not omit its modules")
    for symbol in ("WLCORE", "WLCORE_SDIO", "WL18XX"):
        if f"CONFIG_{symbol}=m" not in configuration.splitlines():
            raise RuntimeError(f"Vendor kernel/module contract changed: {symbol}")
    if include_pl and "CONFIG_UIO_PDRV_GENIRQ=m" not in configuration.splitlines():
        raise RuntimeError("Generic UIO must remain modular and excluded from the PL profile")
    return {symbol: "built-in" for symbol in required}, vendor


def stage_vendor_wifi(root, lock, overlay):
    release = lock["kernel"]
    prefix = f"lib/modules/{release}"
    dependencies = board.inside(root, f"{prefix}/modules.dep").read_text()
    modules = module_closure(dependencies, WIFI_MODULES)
    assets = [(f"{prefix}/{name}", f"{prefix}/{name}") for name in modules]
    assets.extend((f"{prefix}/{name}", f"{prefix}/{name}")
                  for name in ("modules.builtin", "modules.builtin.modinfo", "modules.order"))
    assets.extend((f"lib/firmware/{name}", f"lib/firmware/{name}")
                  for name in ("ti-connectivity/wl18xx-fw-4.bin", "regulatory.db", "regulatory.db.p7s"))
    assets.append(("usr/share/doc/linux-firmware/copyright", "usr/share/licenses/vendor-wifi/copyright"))
    hashes = {}
    for source, relative in assets:
        original = board.inside(root, source)
        destination = overlay / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        hashes[relative] = lab.sha256(destination)
        if relative.endswith(".ko"):
            version = subprocess.check_output(["modinfo", "-F", "vermagic", str(destination)], text=True).split()[0]
            if version != release:
                raise RuntimeError("Vendor Wi-Fi module vermagic differs from the retained kernel")
    lab.run(["depmod", "-b", overlay, release])
    (overlay / "etc").mkdir(parents=True, exist_ok=True)
    (overlay / "etc/axi-lab-vendor-kernel").write_text(release + "\n")
    (overlay / "etc/axi-lab-vendor.sha256").write_text("".join(f"{digest}  /{name}\n" for name, digest in sorted(hashes.items())))
    return dict(kernel=release, modules=modules, file_sha256=hashes,
                file_bytes=sum((overlay / name).stat().st_size for name in hashes))


def build_rootfs(jobs, directory, source_hashes, wifi_config, build=BUILD, native_inputs=None):
    include_pl = bool(native_inputs)
    source = lab.fetch_source("buildroot")
    build.mkdir(parents=True, exist_ok=True)
    private, public = ssh_key()
    overlay = build / "lab-overlay/root/.ssh"
    overlay.mkdir(parents=True, exist_ok=True)
    overlay.chmod(0o700)
    (overlay / "authorized_keys").write_text(public)
    (overlay / "authorized_keys").chmod(0o600)
    release = build / "lab-overlay/etc/axi-lab-release"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(f"AXI_LAB_MIKRO_RECIPE_SHA256={recipe_id(source_hashes, native_inputs)}\n")
    if wifi_config:
        shutil.copyfile(lab.ROOT / wifi_config["path"], build / "lab-overlay/etc/wpa_supplicant-lab.conf")
    else:
        shutil.copyfile(PAYLOAD / "overlay/etc/wpa_supplicant-lab.conf", build / "lab-overlay/etc/wpa_supplicant-lab.conf")
    (build / "lab-overlay/etc/wpa_supplicant-lab.conf").chmod(0o600)
    (build / "lab-overlay/etc/resolv.conf").symlink_to("../run/resolv.conf")
    command = lab.guest_command(source, build)
    lab.logged(command + ["ultra96_mikro_defconfig"], directory / "configure.log")
    if include_pl:
        lab.logged(["env", "CONFIG_=BR2_", "sh", source / "support/kconfig/merge_config.sh", "-m", "-O", build,
                    build / ".config", PAYLOAD / "pl.fragment"], directory / "pl-configure.log")
        lab.logged(command + ["olddefconfig"], directory / "pl-olddefconfig.log")
    lab.logged(command + ["axi-mikro-display-dirclean"], directory / "display-clean.log")
    lab.logged(command + ["busybox-dirclean", "dropbear-dirclean"], directory / "base-clean.log")
    lab.logged(command + [f"BR2_JLEVEL={jobs}", f"-j{jobs}"], directory / "buildroot.log", timeout=1800)
    rootfs = build / "images/rootfs.ext4"
    if rootfs.stat().st_size != ROOTFS_BYTES:
        raise RuntimeError("Mikro rootfs exceeded the fixed 128-MiB size budget")
    forbidden_files = ["usr/bin/Xorg", "usr/bin/firefox", "usr/local/share/pynq-venv"]
    if not include_pl:
        forbidden_files += ["usr/bin/python3", "opt/axi-lab/pl"]
    if list((build / "target/lib/modules").rglob("uio_pdrv_genirq.ko*")):
        raise RuntimeError("Generic UIO must not claim the framefx IRQ")
    for forbidden in forbidden_files:
        if (build / "target" / forbidden).exists():
            raise RuntimeError(f"Unexpected heavyweight component in mikro rootfs: {forbidden}")
    for script in [*(PAYLOAD / "overlay/etc/init.d").iterdir(), *(PAYLOAD / "overlay/usr/sbin").iterdir()]:
        if not script.stat().st_mode & 0o111:
            raise RuntimeError(f"Startup script is not executable: {script}")
    lab.logged(["e2fsck", "-fn", rootfs], directory / "rootfs-check.log", timeout=60)
    metadata = subprocess.check_output(["dumpe2fs", "-h", str(rootfs)], text=True, stderr=subprocess.DEVNULL)
    (directory / "rootfs-metadata.txt").write_text(metadata)
    feature_line = next(line for line in metadata.splitlines() if line.startswith("Filesystem features:"))
    allowed = {"has_journal", "ext_attr", "resize_inode", "dir_index", "filetype", "extent", "64bit", "flex_bg",
               "sparse_super", "large_file", "huge_file", "dir_nlink", "extra_isize", "metadata_csum"}
    if set(feature_line.partition(":")[2].split()) - allowed:
        raise RuntimeError("Root filesystem uses features outside the audited vendor-kernel contract")
    shutil.copyfile(build / ".config", directory / "buildroot.config")
    for name in ("/etc/wpa_supplicant-lab.conf", "/root/.ssh/authorized_keys"):
        metadata = subprocess.check_output(["debugfs", "-R", f"stat {name}", str(rootfs)], text=True, stderr=subprocess.DEVNULL)
        if not re.search(r"Mode:\s+0600\b", metadata):
            raise RuntimeError(f"Private file has unsafe permissions in image: {name}")
    for filename in (rootfs.resolve(), build / "images/rootfs.cpio", build / "images/rootfs.cpio.gz"):
        filename.chmod(0o600)
    return rootfs, dict(private_key=str(private.relative_to(lab.ROOT)),
                        public_key_sha256=hashlib.sha256(public.encode()).hexdigest(),
                        authentication="public key only; password-free local serial console", address="192.168.3.1")


def resized_mbr(original, lock, rootfs_bytes):
    if len(original) != 512 or original[510:] != b"\x55\xaa" or rootfs_bytes <= 0 or rootfs_bytes % 512:
        raise ValueError("Invalid MBR or root filesystem geometry")
    if rootfs_bytes // 512 > 0xffffffff:
        raise ValueError("Root filesystem exceeds MBR sector count")
    for index, partition in enumerate(lock["partitions"]):
        offset = 446 + index * 16
        start, size = struct.unpack_from("<II", original, offset + 8)
        if (start, size, original[offset + 4]) != (partition["start"], partition["size"], int(partition["type"], 16)):
            raise ValueError("Vendor MBR differs from pinned geometry")
    if original[478:510] != bytes(32):
        raise ValueError("Unexpected additional vendor partitions")
    result = bytearray(original)
    struct.pack_into("<I", result, 474, rootfs_bytes // 512)
    return bytes(result)


def copy_exact(source, destination, count):
    if count < 0:
        raise ValueError("Negative image copy length")
    while count:
        chunk = source.read(min(count, 4 * 2**20))
        if not chunk:
            raise RuntimeError("Truncated input during image assembly")
        destination.write(chunk)
        count -= len(chunk)


def assemble(base, rootfs, output, lock):
    offset = lock["partitions"][1]["start"] * 512
    if rootfs.stat().st_size != ROOTFS_BYTES:
        raise ValueError("Rootfs size violates the mikro budget")
    with base.open("rb") as source, output.open("xb") as destination:
        mbr = source.read(512)
        destination.write(resized_mbr(mbr, lock, ROOTFS_BYTES))
        copy_exact(source, destination, offset - 512)
        with rootfs.open("rb") as filesystem:
            copy_exact(filesystem, destination, ROOTFS_BYTES)
    if output.stat().st_size != offset + ROOTFS_BYTES:
        raise RuntimeError("Incorrect assembled image size")
    with base.open("rb") as original, output.open("rb") as candidate:
        original.seek(512)
        candidate.seek(512)
        original_boot = sdcard.digest_prefix(original, offset - 512)
        if sdcard.digest_prefix(candidate, offset - 512) != original_boot:
            raise RuntimeError("Vendor boot bytes changed")
        if sdcard.digest_prefix(candidate, ROOTFS_BYTES) != lab.sha256(rootfs):
            raise RuntimeError("Assembled root filesystem differs from the checked image")
    return dict(offset=512, bytes=offset - 512, sha256=original_boot, mbr_change="p2 sector count only")


def build_image(jobs, wifi_filename=None, pl_native_record=None):
    wifi_config = private_config(wifi_filename)
    native_inputs = pl_inputs(pl_native_record)
    build = BUILD.with_name("ultra96-mikro-pl") if native_inputs else BUILD
    directory = lab.ARTIFACTS / "hardware" / datetime.datetime.now(datetime.timezone.utc).strftime("mikro-%Y%m%dT%H%M%S-%fZ")
    directory.mkdir(parents=True, mode=0o700)
    build.mkdir(parents=True, exist_ok=True)
    build.chmod(0o700)
    overlay = build / "lab-overlay"
    if overlay.is_symlink():
        raise RuntimeError("Refusing a symlinked generated mikro overlay")
    if overlay.exists():
        shutil.rmtree(overlay)
    overlay.mkdir(mode=0o700)
    previous_umask = os.umask(0o077)
    record = dict(schema=1, status="FAIL", stage="H0-mikro-candidate", time_utc=lab.timestamp(),
                  hardware_tested=False, display_tested=False, framefx_pl_included=bool(native_inputs),
                  profile="pl" if native_inputs else "minimal", pl_inputs=native_inputs,
                  flashed=False, directory=str(directory.relative_to(lab.ROOT)), source_sha256=inputs(),
                  wifi_config=wifi_config, contains_wifi_credentials=bool(wifi_config))
    try:
        base, lock = board.fetch_image()
        record["kernel_contract"], record["vendor_wifi"] = kernel_contract(base, lock, directory, build, bool(native_inputs))
        if native_inputs:
            record["pl_bundle"] = stage_pl(lab.ROOT / native_inputs["native_record"], overlay)
        rootfs, record["ssh"] = build_rootfs(jobs, directory, record["source_sha256"], wifi_config, build, native_inputs)
        ramdisk = directory / "rootfs.cpio.gz"
        shutil.copyfile(build / "images/rootfs.cpio.gz", ramdisk)
        host_renderer = directory / "display-host"
        lab.logged(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", PAYLOAD / "display.c", "-o", host_renderer],
                   directory / "host-render-build.log", timeout=60)
        lab.run([host_renderer, "--render", directory / "reference.ppm"])
        record.update(recipe_sha256=recipe_id(record["source_sha256"], native_inputs),
                      renderer_sha256=lab.sha256(build / "target/usr/bin/axi-lab-display"),
                      reference_ppm_sha256=lab.sha256(directory / "reference.ppm"),
                      ramdisk=str(ramdisk.relative_to(lab.ROOT)), ramdisk_sha256=lab.sha256(ramdisk),
                      target_file_bytes=sum(path.stat().st_size for path in (build / "target").rglob("*")
                                            if path.is_file() and not path.is_symlink()))
        output = directory / ("axi-lab-ultra96v1-mikro-pl.img" if native_inputs else "axi-lab-ultra96v1-mikro.img")
        record["preserved_boot_region"] = assemble(base, rootfs, output, lock)
        table = json.loads(subprocess.check_output(["sfdisk", "--json", str(output)], text=True))["partitiontable"]
        expected = {**lock, "partitions": [lock["partitions"][0], {**lock["partitions"][1], "size": ROOTFS_BYTES // 512}]}
        board.validate_partitions(table, expected)
        record["partition_table"] = table
        if inputs() != record["source_sha256"]:
            raise RuntimeError("Mikro source inputs changed during build")
        if private_config(wifi_filename) != wifi_config:
            raise RuntimeError("Private Wi-Fi configuration changed during build")
        if pl_inputs(pl_native_record) != native_inputs:
            raise RuntimeError("PL build/native inputs changed during build")
        record.update(status="PASS", image=str(output.relative_to(lab.ROOT)), image_bytes=output.stat().st_size,
                      image_sha256=lab.sha256(output), rootfs_bytes=ROOTFS_BYTES, rootfs_sha256=lab.sha256(rootfs),
                      vendor_image_sha256=lock["image_sha256"], kernel=lock["kernel"],
                      reduction_percent=round(100 * (1 - output.stat().st_size / lock["image_bytes"]), 2))
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        try:
            lab.write_json(directory / "manifest.json", record)
            lab.write_json(lab.ARTIFACTS / "latest-mikro-image.json", record)
        finally:
            os.umask(previous_umask)
    print(f"MIKRO_IMAGE_PREPARED: {record['image']} ({record['image_bytes']} bytes); NOT FLASHED, board boot/display NOT TESTED")


def validate_smoke(console, candidate):
    lines = console.replace("\r", "").splitlines()
    required = {"AXI_LAB_MIKRO_USERSPACE_PASS hardware=NOT_TESTED",
                "AXI_LAB_MIKRO_SERVICES_PASS wifi=CONFIG_ONLY modules=HASH_ONLY tls=BUILD_ONLY",
                "AXI_LAB_INIT_BEGIN",
                "AXI_LAB_INIT_RESULT service=S30usbgadget status=0",
                f"AXI_LAB_MIKRO_RECIPE_SHA256={candidate['recipe_sha256']}",
                f"{candidate['reference_ppm_sha256']}  /tmp/mikro.ppm",
                f"{candidate['renderer_sha256']}  /usr/bin/axi-lab-display"}
    if candidate.get("framefx_pl_included"):
        required.add("AXI_LAB_MIKRO_PL_BUNDLE_PASS programming=NOT_ATTEMPTED")
    if (not required.issubset(lines) or not re.search(r"^\[\s*[0-9.]+\] reboot: Power down$", console, re.MULTILINE)
            or re.search(r"^AXI_LAB_INIT_RESULT .* status=[1-9][0-9]*$", console, re.MULTILINE)
            or any(failure in console for failure in ("AXI_LAB_MIKRO_USERSPACE_FAIL", "AXI_LAB_INIT_LOG_FAIL",
                                                     "AXI_LAB_USB_FAIL", "Kernel panic", "modprobe:"))):
        raise RuntimeError("Mikro userspace failed its boot, artifact identity, renderer, services or shutdown checks")


def smoke(timeout):
    directory = lab.ARTIFACTS / "hardware" / datetime.datetime.now(datetime.timezone.utc).strftime("mikro-smoke-%Y%m%dT%H%M%S-%fZ")
    directory.mkdir(parents=True)
    qemu = lab.ARTIFACTS / "build/qemu/qemu-system-aarch64"
    kernel = lab.ARTIFACTS / "build/guest/images/Image"
    candidate = json.loads((lab.ARTIFACTS / "latest-mikro-image.json").read_text())
    if not evidence_current(candidate):
        raise RuntimeError("Mikro image is missing, modified or stale; run make board-mikro-image")
    ramdisk = board.inside(lab.ROOT, candidate["ramdisk"])
    command = [qemu, "-machine", "virt-10.2,gic-version=3,dtb-randomness=off", "-accel", "tcg,thread=single",
               "-cpu", "cortex-a53", "-smp", "1", "-m", "512M", "-display", "none", "-monitor", "none",
               "-serial", "stdio", "-nic", "none", "-no-reboot", "-kernel", kernel, "-initrd", ramdisk,
               "-append", "console=ttyAMA0 rdinit=/sbin/init panic=-1 axi_lab.mikro_smoke=1"]
    record = dict(schema=1, status="FAIL", stage="mikro-userspace-smoke", hardware_tested=False,
                  vendor_kernel_tested=False, time_utc=lab.timestamp(), source_sha256=inputs(),
                  image_sha256=candidate["image_sha256"])
    try:
        record["binary_sha256"] = {str(filename.relative_to(lab.ROOT)): lab.sha256(filename) for filename in (qemu, kernel, ramdisk)}
        lab.logged(command, directory / "console.log", timeout=timeout)
        console = (directory / "console.log").read_text(errors="replace")
        validate_smoke(console.replace("\r", ""), candidate)
        record["status"] = "PASS"
        if not smoke_current(record, candidate):
            record["status"] = "FAIL"
            raise RuntimeError("Mikro smoke inputs changed during execution")
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        lab.write_json(directory / "result.json", record)
        lab.write_json(lab.ARTIFACTS / "latest-mikro-smoke.json", {**record, "directory": str(directory.relative_to(lab.ROOT))})
    print("MIKRO_USERSPACE_SMOKE_PASS; uses the simulation kernel, not the vendor board kernel or real DisplayPort")
