#!/usr/bin/env python3
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
SOURCES = ROOT / "third_party"
LOCK = json.loads((ROOT / "versions.lock").read_text())


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def project_hashes():
    paths = [ROOT / "versions.lock", ROOT / "Makefile"]
    for directory in ("scripts", "linux", "tests", "fpga", "qemu", "transport", "sim", "rtl"):
        paths.extend(path for path in (ROOT / directory).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    return {str(path.relative_to(ROOT)): sha256(path) for path in sorted(paths)}


def run(command, **kwargs):
    command = [str(item) for item in command]
    print("+ " + shlex.join(command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def capture(command, timeout=15):
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
        return {"returncode": result.returncode, "output": result.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"returncode": -1, "output": str(error)}


def build_environment():
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "TZ": "UTC"})
    environment["PATH"] = "/usr/bin:/bin:" + environment.get("PATH", "")
    for name in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "CROSS_COMPILE",
                 "CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS", "CONFIG_SITE"):
        environment.pop(name, None)
    return environment


def verify_archive(path, expected):
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(f"SHA256 mismatch for {path}: expected {expected}, got {actual}")


def extract_archive(archive, destination, directory, excluded=()):
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        with tarfile.open(archive) as bundle:
            members = [member for member in bundle.getmembers() if member.name not in excluded]
            for member in members:
                parts = Path(member.name).parts
                if not parts or parts[0] != directory or ".." in parts:
                    raise RuntimeError(f"Unexpected archive path: {member.name}")
                if member.issym() or member.islnk():
                    target = member.linkname
                    if member.issym():
                        target = posixpath.join(posixpath.dirname(member.name), target)
                    target_parts = Path(posixpath.normpath(target)).parts
                    if not target_parts or target_parts[0] != directory:
                        raise RuntimeError(f"Archive link escapes source root: {member.name}")
            bundle.extractall(temporary, members=members, filter="data")
        (Path(temporary) / directory).rename(destination)


def fetch_source(name):
    source = LOCK["sources"][name]
    destination = SOURCES / source["directory"]
    SOURCES.mkdir(exist_ok=True)
    if source["kind"] == "archive":
        downloads = ARTIFACTS / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        archive = downloads / source["url"].rsplit("/", 1)[1]
        if not archive.exists():
            partial = archive.with_suffix(archive.suffix + ".part")
            run(["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "30",
                 "--max-time", "1800", "--output", partial, source["url"]])
            verify_archive(partial, source["sha256"])
            partial.replace(archive)
        verify_archive(archive, source["sha256"])
        if not destination.exists():
            extract_archive(archive, destination, source["directory"], source.get("excluded_archive_members", ()))
        print(f"{name}: archive SHA256 verified; {destination}")
    else:
        if not destination.exists():
            with tempfile.TemporaryDirectory(dir=SOURCES) as temporary:
                run(["git", "init", "--quiet", temporary])
                run(["git", "-C", temporary, "remote", "add", "origin", source["url"]])
                run(["git", "-C", temporary, "fetch", "--depth", "1", "origin", source["commit"]])
                run(["git", "-C", temporary, "checkout", "--quiet", "--detach", "FETCH_HEAD"])
                Path(temporary).rename(destination)
        revision = capture(["git", "-C", str(destination), "rev-parse", "HEAD"])
        status = capture(["git", "-C", str(destination), "status", "--porcelain"])
        if (revision["returncode"] or revision["output"] != source["commit"]
                or status["returncode"] or status["output"]):
            raise RuntimeError(f"{name}: checkout differs from lock; preserve/review it manually")
        print(f"{name}: clean pinned commit {source['commit']}")
    return destination


def vivado_path():
    override = os.environ.get("VIVADO")
    if override:
        return override
    installed = shutil.which("vivado")
    if installed:
        return installed
    candidates = []
    for directory in (Path.home() / "Applications/vivado", Path("/tools/Xilinx"),
                      Path("/opt/Xilinx"), Path("/tools/AMD"), Path("/opt/AMD")):
        candidates.extend(directory.glob("*/Vivado/bin/vivado"))
        candidates.extend(directory.glob("Vivado/*/bin/vivado"))
    return str(sorted(candidates)[-1]) if candidates else None


def doctor():
    required = ["git", "gcc", "g++", "make", "ninja", "pkg-config", "curl", "wget",
                "tar", "xz", "bzip2", "gzip", "unzip", "cpio", "rsync", "bc", "patch",
                "perl", "bison", "flex", "file", "which", "sed", "awk", "diff", "find", "verilator"]
    missing = [name for name in required if not shutil.which(name)]
    versions = {}
    for name in ("gcc", "g++", "make", "git", "ninja", "meson", "verilator", "qemu-system-aarch64"):
        location = shutil.which(name)
        versions[name] = {"path": location, **(capture([location, "--version"]) if location else {})}
    dependencies = capture(["pkg-config", "--modversion", "glib-2.0", "pixman-1", "zlib"])
    if dependencies["returncode"]:
        missing.append("glib-2.0/pixman-1/zlib development packages")
    compiler_probe = capture(["sh", "-c", "printf '#include <glib.h>\\nint main(void) { return 0; }\\n' | gcc -x c -fsyntax-only $(pkg-config --cflags glib-2.0) -"])
    if compiler_probe["returncode"]:
        missing.append("working C compiler and glib headers")
    xattr_probe = capture(["sh", "-c", "printf '#include <sys/xattr.h>\\nint main(void) { return 0; }\\n' | gcc -x c -fsyntax-only -"])
    if xattr_probe["returncode"]:
        missing.append("host xattr headers for QEMU virtio-9p")
    uapi_probe = capture(["gcc", "-m32", "-std=c11", "-fsyntax-only", str(ROOT / "tests/framefx_uapi.c")])
    if uapi_probe["returncode"]:
        missing.append("32-bit UAPI compile headers (gcc-multilib on Ubuntu)")
    venv_probe = capture([sys.executable, "-c", "import ensurepip; import venv"])
    if venv_probe["returncode"]:
        missing.append("Python venv/ensurepip (python3-venv on Debian/Ubuntu)")
    vivado = vivado_path()
    disk = shutil.disk_usage(ROOT)
    report = {"schema": 1, "time_utc": timestamp(), "platform": platform.platform(),
              "os_release": platform.freedesktop_os_release(), "python": sys.version,
              "python_path": sys.executable, "cpu_count": os.cpu_count(),
              "disk_free_bytes": disk.free, "tools": versions,
              "pkg_config": dependencies, "compiler_probe": compiler_probe, "venv_probe": venv_probe,
              "xattr_probe": xattr_probe, "uapi32_probe": uapi_probe,
              "vivado": {"path": vivado, **(capture([vivado, "-version"], 30) if vivado else {})},
              "petalinux": shutil.which("petalinux-build"), "missing_required": missing,
              "status": "PASS" if not missing else "FAIL"}
    write_json(ARTIFACTS / "host/doctor.json", report)
    print(f"Host: {report['os_release']['PRETTY_NAME']}; {report['cpu_count']} CPUs; {disk.free / 2**30:.1f} GiB free")
    for name, version in versions.items():
        print(f"{name}: {version.get('output', 'not on PATH').splitlines()[0]}")
    print(f"Vivado: {vivado or 'not found'} (license and board compatibility require fpga-inventory)")
    print("Build prerequisites: " + (", ".join(missing) if missing else "PASS"))
    print("QEMU/cross-toolchain are built locally; missing system copies are optional.")
    print("Evidence: artifacts/host/doctor.json")
    if disk.free < 20 * 2**30:
        print("WARNING: less than 20 GiB free; guest builds may run out of disk.")
    if missing:
        raise RuntimeError("Missing host dependencies; see docs/setup.md")


def logged(command, logfile, cwd=ROOT, timeout=None):
    logfile.parent.mkdir(parents=True, exist_ok=True)
    command = [str(item) for item in command]
    print("+ " + shlex.join(command), flush=True)
    print(f"  log: {logfile.relative_to(ROOT)}", flush=True)
    with logfile.open("w") as stream:
        stream.write(shlex.join(command) + "\n")
        stream.flush()
        process = subprocess.Popen(command, cwd=cwd, env=build_environment(),
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            result = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if result:
        print("\n".join(logfile.read_text(errors="replace").splitlines()[-35:]))
        raise RuntimeError(f"Command exited {result}; see {logfile}")


def build_qemu(jobs):
    source = fetch_source("qemu")
    run([sys.executable, ROOT / "qemu/integrate.py", source])
    build = ARTIFACTS / "build/qemu"
    build.mkdir(parents=True, exist_ok=True)
    configuration = [str(source / "configure"), "--target-list=aarch64-softmmu", "--without-default-features",
                     "--enable-tcg", "--enable-fdt", "--enable-virtfs", "--enable-attr", "--disable-werror", "--disable-docs",
                     f"--python={sys.executable}"]
    stamp = build / "lab-configure.json"
    previous = json.loads(stamp.read_text()) if stamp.exists() else None
    if not (build / "build.ninja").exists() or previous != configuration:
        logged(configuration, ARTIFACTS / "logs/qemu-configure.log", cwd=build)
        write_json(stamp, configuration)
    logged(["ninja", "-C", build, f"-j{jobs}", "qemu-system-aarch64"], ARTIFACTS / "logs/qemu-build.log")
    print(capture([str(build / "qemu-system-aarch64"), "--version"])["output"])


def guest_command(source, build):
    return ["make", "-C", source, f"O={build}", f"BR2_EXTERNAL={ROOT / 'linux/buildroot'}",
            f"BR2_DL_DIR={ARTIFACTS / 'downloads/buildroot'}"]


def build_guest(jobs, legal=False):
    source = fetch_source("buildroot")
    build = ARTIFACTS / "build/guest"
    build.mkdir(parents=True, exist_ok=True)
    command = guest_command(source, build)
    logged(command + ["framefx_virt_defconfig"], ARTIFACTS / "logs/guest-configure.log")
    if not legal:
        logged(command + ["axi-spike-dirclean"], ARTIFACTS / "logs/spike-driver-clean.log")
        logged(command + ["framefx-dirclean"], ARTIFACTS / "logs/framefx-driver-clean.log")
        kernel_stamp = build / "lab-linux-config.sha256"
        kernel_hash = sha256(ROOT / "linux/buildroot/linux.config")
        if kernel_stamp.exists() and kernel_stamp.read_text() != kernel_hash:
            logged(command + ["linux-dirclean"], ARTIFACTS / "logs/linux-clean.log")
        elif not kernel_stamp.exists() and (build / f"build/linux-{LOCK['guest']['linux']}").exists():
            logged(command + ["linux-dirclean"], ARTIFACTS / "logs/linux-clean.log")
        logged(command + [f"BR2_JLEVEL={jobs}", f"-j{jobs}", "linux"], ARTIFACTS / "logs/linux-build.log")
        kernel_stamp.write_text(kernel_hash)
    logged(command + [f"BR2_JLEVEL={jobs}", f"-j{jobs}", "legal-info" if legal else "all"],
           ARTIFACTS / ("logs/guest-legal.log" if legal else "logs/guest-build.log"))


def boot_command(qemu, images, smoke=True):
    return [str(qemu), "-machine", "virt-10.2,gic-version=3,dtb-randomness=off",
            "-accel", "tcg,thread=single", "-cpu", "cortex-a53", "-smp", "1", "-m", "512M",
            "-display", "none", "-monitor", "none", "-serial", "stdio", "-nic", "none",
            "-no-reboot", "-kernel", str(images / "Image"), "-initrd", str(images / "rootfs.cpio.gz"),
            "-append", "console=ttyAMA0 rdinit=/init panic=-1" + (" axi_lab.smoke=1" if smoke else "")]


def validate_console(console):
    if not re.search(r"^AXI_LAB_BOOT_PASS\r?$", console, re.MULTILINE):
        raise RuntimeError("Guest did not report boot success")
    if "reboot: Power down" not in console:
        raise RuntimeError("Guest did not power off")
    if "Kernel panic" in console or "AXI_LAB_BOOT_FAIL" in console:
        raise RuntimeError("Guest reported a failure")
    if re.search(r"^FAIL\r?$", console, re.MULTILINE) or "can't open /dev/" in console:
        raise RuntimeError("Guest startup service or device-node failure")


def boot_smoke(timeout):
    qemu = ARTIFACTS / "build/qemu/qemu-system-aarch64"
    images = ARTIFACTS / "build/guest/images"
    for required in (qemu, images / "Image", images / "rootfs.cpio.gz"):
        if not required.exists():
            raise RuntimeError(f"Missing {required}; run make qemu guest")
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("boot-%Y%m%dT%H%M%S-%fZ")
    evidence = ARTIFACTS / run_id
    evidence.mkdir(parents=True)
    command = boot_command(qemu, images)
    write_json(evidence / "command.json", command)
    (evidence / "command.sh").write_text(shlex.join(command) + "\n")
    result = {"schema": 1, "test": "stock-aarch64-linux-boot", "time_utc": timestamp(),
              "status": "FAIL", "milestone": "M1 prerequisite only", "accelerator_tested": False,
              "timeout_seconds": timeout}
    started = time.monotonic()
    try:
        dump = command.copy()
        dump[2] += f",dumpdtb={evidence / 'virt.dtb'}"
        logged(dump, evidence / "dt-dump.log", timeout=30)
        logged(command, evidence / "qemu-console.log", timeout=timeout)
        console = (evidence / "qemu-console.log").read_text(errors="replace")
        validate_console(console)
        dtc = ARTIFACTS / "build/guest/host/bin/dtc"
        logged([dtc, "-I", "dtb", "-O", "dts", "-o", evidence / "virt.dts", evidence / "virt.dtb"],
               evidence / "dtc.log", timeout=30)
        shutil.copy2(ARTIFACTS / "build/guest/.config", evidence / "buildroot.config")
        kernel = ARTIFACTS / f"build/guest/build/linux-{LOCK['guest']['linux']}/.config"
        shutil.copy2(kernel, evidence / "linux.config")
        versions = {"lock": LOCK, "project_sha256": project_hashes(),
                    "qemu": capture([str(qemu), "--version"]),
                    "sha256": {str(path.relative_to(ROOT)): sha256(path)
                               for path in (ROOT / "versions.lock", qemu, images / "Image", images / "rootfs.cpio.gz")}}
        write_json(evidence / "versions.json", versions)
        result["status"] = "PASS"
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(evidence / "result.json", result)
        write_json(ARTIFACTS / "latest-boot.json", {"directory": str(evidence.relative_to(ROOT)), **result})
        print(f"{result['status']}: {evidence.relative_to(ROOT)}")


def fpga_inventory():
    vivado = vivado_path()
    if not vivado:
        raise RuntimeError("Vivado not found; set VIVADO=/absolute/path/to/bin/vivado")
    build = ARTIFACTS / "host/vivado"
    build.mkdir(parents=True, exist_ok=True)
    logged([vivado, "-mode", "batch", "-nojournal", "-nolog", "-source",
            ROOT / "fpga/ultra96-v1/inventory.tcl", "-tclargs",
            SOURCES / "avnet-bdf", SOURCES / "avnet-bdf-ultra96v1"],
           ARTIFACTS / "host/vivado-inventory.log", cwd=build, timeout=120)


def toolcheck(jobs):
    build = ARTIFACTS / "build/toolcheck"
    build.mkdir(parents=True, exist_ok=True)
    source = ROOT / "tests/fixtures/toolcheck.sv"
    result = {"schema": 1, "test": "verilator-toolchain", "time_utc": timestamp(),
              "status": "FAIL", "accelerator_tested": False,
              "verilator": capture(["verilator", "--version"]), "source_sha256": sha256(source)}
    try:
        logged(["verilator", "--lint-only", "--Wall", source], ARTIFACTS / "logs/toolcheck-lint.log", timeout=30)
        logged(["verilator", "--cc", "--exe", "--build", "--Wall", "--Mdir", build,
                "--top-module", "toolcheck", "-j", str(jobs), source,
                ROOT / "tests/fixtures/toolcheck.cpp"], ARTIFACTS / "logs/toolcheck-build.log", timeout=180)
        logged([build / "Vtoolcheck"], ARTIFACTS / "logs/toolcheck-run.log", timeout=10)
        result["status"] = "PASS"
    finally:
        write_json(ARTIFACTS / "toolcheck.json", result)
    print("PASS: Verilator reset/clock/C++ toolchain check (not accelerator RTL)")


def fpga_toolcheck():
    vivado = vivado_path()
    if not vivado:
        raise RuntimeError("Vivado not found; set VIVADO=/absolute/path/to/bin/vivado")
    build = ARTIFACTS / "build/fpga-toolcheck"
    build.mkdir(parents=True, exist_ok=True)
    result = {"schema": 1, "test": "vivado-counter-synthesis", "status": "FAIL",
              "time_utc": timestamp(), "part": "xczu3eg-sbva484-1-e", "hardware_tested": False,
              "vivado": capture([vivado, "-version"], 30)}
    try:
        logged([vivado, "-mode", "batch", "-nojournal", "-nolog", "-source",
                ROOT / "fpga/ultra96-v1/toolcheck.tcl", "-tclargs",
                ROOT / "tests/fixtures/toolcheck.sv"], ARTIFACTS / "logs/fpga-toolcheck.log",
               cwd=build, timeout=180)
        if not (build / "toolcheck-utilization.rpt").exists():
            raise RuntimeError("Vivado did not produce the synthesis report")
        console = (ARTIFACTS / "logs/fpga-toolcheck.log").read_text(errors="replace")
        if not re.search(r"^AXI_LAB_SYNTHESIS_PASS xczu3eg-sbva484-1-e COUNTER_ONLY\r?$", console, re.MULTILINE):
            raise RuntimeError("Vivado did not reach the synthesis success marker")
        result["status"] = "PASS"
    finally:
        write_json(ARTIFACTS / "fpga-toolcheck.json", result)
    print("PASS: counter synthesis for V1 part (not board implementation or hardware validation)")


def test_foundation():
    result = {"schema": 1, "test": "foundation-regressions", "time_utc": timestamp(), "status": "FAIL"}
    try:
        logged([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
               ARTIFACTS / "logs/tests.log", timeout=60)
        result["status"] = "PASS"
    finally:
        write_json(ARTIFACTS / "tests.json", result)
    print("PASS: tooling/transport regressions; artifacts/logs/tests.log")


def evidence_current(record, project):
    if record.get("status") != "PASS" or not record.get("directory"):
        return False
    try:
        versions = json.loads((ROOT / record["directory"] / "versions.json").read_text())
        binaries = versions["binary_sha256"]
        return versions["project_sha256"] == project and bool(binaries) and all(
            (ROOT / name).is_file() and sha256(ROOT / name) == digest
            for name, digest in binaries.items())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def report():
    evidence = {}
    for name, path in (("host", ARTIFACTS / "host/doctor.json"),
                       ("stock_boot", ARTIFACTS / "latest-boot.json"),
                       ("concurrency_spike", ARTIFACTS / "latest-spike.json"),
                       ("standalone_fabric", ARTIFACTS / "latest-rtl.json"),
                       ("framefx_rtl", ARTIFACTS / "latest-framefx.json"),
                       ("linux_framefx", ARTIFACTS / "latest-demo.json"),
                       ("benchmark_sim", ARTIFACTS / "latest-benchmark-sim.json"),
                       ("platform_stress", ARTIFACTS / "latest-stress.json"),
                       ("control_compat_hardening", ARTIFACTS / "latest-hardening.json"),
                       ("framefx_synthesis", ARTIFACTS / "framefx-synth.json"),
                       ("verilator_toolchain", ARTIFACTS / "toolcheck.json"),
                       ("tests", ARTIFACTS / "tests.json"),
                       ("board_image", ARTIFACTS / "latest-board-image.json"),
                       ("mikro_image", ARTIFACTS / "latest-mikro-image.json"),
                       ("mikro_userspace", ARTIFACTS / "latest-mikro-smoke.json"),
                       ("mikro_usb", ARTIFACTS / "latest-mikro-usb.json"),
                       ("usb_userspace", ARTIFACTS / "latest-usb-check.json"),
                       ("ultra96_pl_build", ARTIFACTS / "ultra96-pl-bitstream.json"),
                       ("ultra96_pl_attempt", ARTIFACTS / "latest-pl-attempt.json"),
                       ("ultra96_pl_dma", ARTIFACTS / "latest-pl-dma.json"),
                       ("ultra96_pl_benchmark", ARTIFACTS / "latest-pl-benchmark.json"),
                       ("vivado_toolchain", ARTIFACTS / "fpga-toolcheck.json")):
        evidence[name] = json.loads(path.read_text()) if path.exists() else {"status": "NOT_RUN"}
    spike = evidence["concurrency_spike"]
    fabric = evidence["standalone_fabric"]
    framefx = evidence["framefx_rtl"]
    demo = evidence["linux_framefx"]
    project = project_hashes()
    current = evidence_current(spike, project)
    spike["inputs_current"] = current
    fabric["inputs_current"] = evidence_current(fabric, project)
    framefx["inputs_current"] = evidence_current(framefx, project)
    demo["inputs_current"] = evidence_current(demo, project)
    evidence["benchmark_sim"]["inputs_current"] = evidence_current(evidence["benchmark_sim"], project)
    evidence["platform_stress"]["inputs_current"] = evidence_current(evidence["platform_stress"], project)
    evidence["control_compat_hardening"]["inputs_current"] = evidence_current(evidence["control_compat_hardening"], project)
    synthesis = evidence["framefx_synthesis"]
    import board
    import board_usb
    import mikro
    import mikro_usb
    import pl
    evidence["ultra96_pl_build"]["inputs_current"] = pl.evidence_current(evidence["ultra96_pl_build"])
    evidence["ultra96_pl_attempt"]["historical_observation_only"] = True
    evidence["ultra96_pl_dma"]["historical_observation_only"] = True
    evidence["ultra96_pl_benchmark"]["historical_observation_only"] = True
    evidence["usb_userspace"]["inputs_current"] = board_usb.evidence_current(evidence["usb_userspace"])
    evidence["board_image"]["inputs_current"] = board.evidence_current(evidence["board_image"])
    evidence["mikro_image"]["inputs_current"] = mikro.evidence_current(evidence["mikro_image"])
    evidence["mikro_userspace"]["inputs_current"] = mikro.smoke_current(evidence["mikro_userspace"], evidence["mikro_image"])
    evidence["mikro_usb"]["inputs_current"] = mikro_usb.evidence_current(evidence["mikro_usb"])
    evidence["mikro_usb"]["historical_observation_only"] = True
    synthesis["inputs_current"] = synthesis.get("status") == "PASS" and bool(synthesis.get("source_sha256")) and all(
        (ROOT / name).is_file() and sha256(ROOT / name) == digest for name, digest in synthesis.get("source_sha256", {}).items())
    evidence.update({"schema": 1, "time_utc": timestamp(), "lock_sha256": sha256(ROOT / "versions.lock"),
                     "M1": "PASS" if current else "NOT_CURRENTLY_VERIFIED: run make spike-test",
                     "M2": "PASS" if current and fabric["inputs_current"] else "NOT_CURRENTLY_VERIFIED: run make rtl-test spike-test",
                     "M3": "PASS" if current and fabric["inputs_current"] and framefx["inputs_current"]
                     else "NOT_CURRENTLY_VERIFIED: run make framefx-test rtl-test spike-test",
                     "M4": "PASS" if current and fabric["inputs_current"] and framefx["inputs_current"] and demo["inputs_current"]
                     else "NOT_CURRENTLY_VERIFIED: run make rtl-test spike-test demo-sim",
                     "M5": ("PARTIAL_BUILD_VERIFIED: SD/display acceptance incomplete; see ultra96_pl_attempt and ultra96_pl_dma"
                            if evidence["ultra96_pl_build"]["inputs_current"] else "NOT_CURRENTLY_VERIFIED: run make fpga"),
                     "M5_M7": "NOT_COMPLETE",
                     "physical_validation": ("PARTIAL_H0_USB: display pending; PL/CSR/DMA/IRQ acceptance tracked separately"
                                             if evidence["usb_userspace"]["inputs_current"]
                                             else "NO_CURRENT_USB_EVIDENCE: not a hardware failure result")})
    write_json(ARTIFACTS / "report.json", evidence)
    print(json.dumps({name: item.get("status") for name, item in evidence.items() if isinstance(item, dict)}, indent=2))
    print(f"M1: {evidence['M1']}; M2: {evidence['M2']}; M3: {evidence['M3']}; M4: {evidence['M4']}")
    print("Full evidence: artifacts/report.json; this is not a hardware PASS.")


def positive_integer(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    if any(character.isspace() for character in str(ROOT)):
        raise RuntimeError("Buildroot requires a repository path without whitespace")
    parser = argparse.ArgumentParser(description="axi-cosim-lab foundation tooling")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("doctor", "report", "fpga-inventory", "fpga-toolcheck", "fpga", "fpga-validate", "test", "guest-shell", "rtl-lint", "framefx-synth", "board-image"):
        commands.add_parser(action)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("names", nargs="+", choices=list(LOCK["sources"]))
    for action in ("qemu", "guest", "legal-info", "toolcheck", "spike-build", "rtl-test", "framefx-test", "demo-build", "fpga-wrapper-test"):
        commands.add_parser(action).add_argument("--jobs", type=positive_integer, default=8)
    mikro_parser = commands.add_parser("board-mikro-image")
    mikro_parser.add_argument("--jobs", type=positive_integer, default=4)
    mikro_parser.add_argument("--wifi-config", type=Path)
    mikro_parser.add_argument("--pl-native-record", type=Path)
    commands.add_parser("boot-smoke").add_argument("--timeout", type=positive_integer, default=180)
    commands.add_parser("spike-test").add_argument("--timeout", type=positive_integer, default=60)
    commands.add_parser("demo-sim").add_argument("--timeout", type=positive_integer, default=180)
    commands.add_parser("benchmark-sim").add_argument("--timeout", type=positive_integer, default=180)
    commands.add_parser("hardening-test").add_argument("--timeout", type=positive_integer, default=180)
    commands.add_parser("board-mikro-smoke").add_argument("--timeout", type=positive_integer, default=90)
    stress_parser = commands.add_parser("stress-test")
    stress_parser.add_argument("--jobs", type=positive_integer, default=8)
    stress_parser.add_argument("--cases", type=positive_integer, default=512)
    stress_parser.add_argument("--linux-jobs", type=positive_integer, default=256)
    stress_parser.add_argument("--seed", type=positive_integer, default=2026)
    stress_parser.add_argument("--timeout", type=positive_integer, default=600)
    arguments = parser.parse_args()
    ARTIFACTS.mkdir(exist_ok=True)
    with (ARTIFACTS / ".lab.lock").open("w") as lockfile:
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another lab command is running; wait for it to finish") from None
        if arguments.action == "fetch":
            for name in arguments.names:
                fetch_source(name)
        elif arguments.action == "doctor":
            doctor()
        elif arguments.action == "qemu":
            build_qemu(arguments.jobs)
        elif arguments.action in ("guest", "legal-info"):
            build_guest(arguments.jobs, legal=arguments.action == "legal-info")
        elif arguments.action == "boot-smoke":
            boot_smoke(arguments.timeout)
        elif arguments.action == "spike-build":
            import spike
            spike.build(arguments.jobs)
        elif arguments.action == "spike-test":
            import spike
            spike.test(arguments.timeout)
        elif arguments.action == "demo-build":
            import demo
            demo.build(arguments.jobs)
        elif arguments.action == "demo-sim":
            import demo
            demo.test(arguments.timeout)
        elif arguments.action == "benchmark-sim":
            import pl_benchmark
            pl_benchmark.simulation(arguments.timeout)
        elif arguments.action == "stress-test":
            import stress
            stress.test(arguments.jobs, arguments.cases, arguments.linux_jobs, arguments.seed, arguments.timeout)
        elif arguments.action == "hardening-test":
            import hardening
            hardening.test(arguments.timeout)
        elif arguments.action == "rtl-lint":
            import rtl
            import framefx
            rtl.lint()
            framefx.lint()
        elif arguments.action == "rtl-test":
            import rtl
            import framefx
            rtl.test(arguments.jobs)
            framefx.test(arguments.jobs)
        elif arguments.action == "framefx-test":
            import framefx
            framefx.test(arguments.jobs)
        elif arguments.action == "framefx-synth":
            import framefx
            framefx.synthesize()
        elif arguments.action == "guest-shell":
            run(boot_command(ARTIFACTS / "build/qemu/qemu-system-aarch64",
                             ARTIFACTS / "build/guest/images", smoke=False))
        elif arguments.action == "fpga-inventory":
            fpga_inventory()
        elif arguments.action == "toolcheck":
            toolcheck(arguments.jobs)
        elif arguments.action == "fpga-toolcheck":
            fpga_toolcheck()
        elif arguments.action in ("fpga", "fpga-validate"):
            import pl
            pl.build("validate" if arguments.action == "fpga-validate" else "bitstream")
        elif arguments.action == "fpga-wrapper-test":
            import pl
            pl.test_wrapper(arguments.jobs)
        elif arguments.action == "test":
            test_foundation()
        elif arguments.action == "report":
            report()
        elif arguments.action == "board-image":
            import board
            board.build_image()
        elif arguments.action == "board-mikro-image":
            import mikro
            mikro.build_image(arguments.jobs, arguments.wifi_config, arguments.pl_native_record)
        elif arguments.action == "board-mikro-smoke":
            import mikro
            mikro.smoke(arguments.timeout)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, tarfile.TarError) as failure:
        print(f"ERROR: {failure}", file=sys.stderr)
        sys.exit(1)
