import datetime
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import demo
import lab


CONTROL_CASES = (
    ("caps-disconnect", "caps", "mmio-disconnect:2:4:2"),
    ("submit-idle-disconnect", "submit", "mmio-disconnect:2:0x10:2"),
    ("configure-disconnect", "submit", "mmio-disconnect:3:0x28:1"),
    ("start-disconnect", "submit", "mmio-disconnect:3:0x0c:1"),
    ("reset-disconnect", "reset", "mmio-disconnect:3:0x0c:1"),
    ("reset-idle-disconnect", "reset", "mmio-disconnect:2:0x10:2"),
    ("completion-disconnect", "wait", "mmio-disconnect:2:0x50:1"),
    ("caps-corrupt", "caps", "mmio-corrupt:4:2:0xffffffff"),
    ("capabilities-corrupt", "caps", "mmio-corrupt:8:1:0"),
    ("submit-idle-corrupt", "submit", "mmio-corrupt:0x10:2:0x80"),
    ("reset-idle-corrupt", "reset", "mmio-corrupt:0x10:2:0x80"),
    ("irq-pending-corrupt", "wait", "mmio-corrupt:0x3c:1:4"),
    ("irq-state-corrupt", "wait", "mmio-corrupt:0x10:3:0x80"),
    ("irq-error-flag-corrupt", "wait", "mmio-corrupt:0x10:3:6"),
    ("irq-error-pending-corrupt", "wait", "mmio-corrupt:0x3c:1:2"),
    ("irq-error-corrupt", "wait", "mmio-corrupt:0x44:1:99"),
    ("irq-read-corrupt", "wait", "mmio-corrupt:0x50:1:0xffffffff"),
    ("irq-written-corrupt", "wait", "mmio-corrupt:0x54:1:1"),
)


def validate_control(console, simulator, operation, terminal):
    required = (f"FRAMEFX_CONTROL_PASS operation={operation}", "FRAMEFX_KERNEL_DEBUG_PASS",
                "FRAMEFX_CONTROL_QUARANTINE_PASS" if terminal else "FRAMEFX_CONTROL_RELOAD_PASS")
    if not all(marker in console for marker in required) or "FRAMEFX_CONTROL_FAIL" in console:
        raise RuntimeError("Missing control-path failure evidence")
    if simulator.count("RTL_INJECTED_MMIO") != 1:
        raise RuntimeError("Missing or repeated MMIO fault injection")
    expected = "FRAMEFX_CONTROL_CMA allocated_pages=4 released_pages=" + ("0" if terminal else "4")
    if expected not in console:
        raise RuntimeError("Incorrect control-fault allocation accounting")
    if terminal:
        if console.count("AXI_COSIM_FAULT") != 1 or "FRAMEFX_QUARANTINE" not in console:
            raise RuntimeError("Missing terminal control fault")
        if "AXI_DMA" in console[console.index("AXI_COSIM_FAULT"):]:
            raise RuntimeError("Post-fault DMA")
    elif "AXI_COSIM_FAULT" in console or "FRAMEFX_QUARANTINE" in console or "RTL_FAILURE" in simulator:
        raise RuntimeError("Corrupt telemetry did not reach a proven idle shutdown")
    return dict(operation=operation, terminal=terminal, allocated_pages=4, released_pages=0 if terminal else 4,
                dma_requests=len(re.findall(r"AXI_DMA ", console)))


def build_compat():
    for tool in ("clang", "ld.lld"):
        if not shutil.which(tool, path=lab.build_environment()["PATH"]):
            raise RuntimeError(f"hardening-test requires {tool} for the freestanding ARM32 fixture")
    directory = lab.ARTIFACTS / "build/compat32"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "framefx-compat32"
    sysroot = lab.ARTIFACTS / "build/guest/host/aarch64-buildroot-linux-gnu/sysroot"
    lab.logged(["clang", "--target=armv7-linux-gnueabi", "-march=armv7-a", "-marm", "-std=c11",
                "-O2", "-Wall", "-Wextra", "-Werror", "-ffreestanding", "-fno-builtin", "-fno-stack-protector",
                "-fomit-frame-pointer", "-nostdlib", "-static", "-fuse-ld=lld", "-Wl,-e,_start", "-Wl,-z,noexecstack",
                "-isystem", sysroot / "usr/include", lab.ROOT / "tests/framefx_compat32.c", "-o", binary],
               lab.ARTIFACTS / "logs/compat32-build.log", timeout=60)
    header = binary.read_bytes()[:20]
    if header[:6] != b"\x7fELF\x01\x01" or int.from_bytes(header[18:20], "little") != 40:
        raise RuntimeError("Compatibility fixture is not little-endian ELF32 ARM")
    return binary


def validate_compat(console, simulator):
    required = ("FRAMEFX_COMPAT32_PASS jobs=8 modes=4 pointer_bits=32", "FRAMEFX_KERNEL_DEBUG_PASS",
                "FRAMEFX_CONTROL_RELOAD_PASS", "FRAMEFX_CONTROL_CMA allocated_pages=4 released_pages=4")
    if not all(marker in console for marker in required) or any(
            marker in console for marker in ("FRAMEFX_COMPAT32_FAIL", "AXI_COSIM_FAULT", "FRAMEFX_QUARANTINE")):
        raise RuntimeError("Missing runtime ARM32 ABI/lifetime/debug evidence")
    if "RTL_SUMMARY" not in simulator or "RTL_FAILURE" in simulator or "RTL_INJECTED" in simulator:
        raise RuntimeError("Compatibility simulator did not finish cleanly")
    return dict(images=8, modes=4, pointer_bits=32, allocated_pages=4, released_pages=4)


def validate_cli(evidence):
    binary = lab.ARTIFACTS / "build/framefx-socket/Vframefx_core"
    cases = [(0, value) for value in ("0", "-1", "1junk", "18446744073709551616")]
    cases += [(1, value) for value in ("-1", "4294967296", "1junk")]
    cases += [(2, value) for value in ("-1", "1065", "3junk")]
    cases += [(3, value) for value in ("unknown", "none:2", "disconnect:0", "stall:1junk", "partial:",
                                      "mmio-corrupt:4:2:4294967296", "mmio-disconnect:2:3:1")]
    results = []
    with tempfile.TemporaryDirectory(prefix="invalid-framefx-") as temporary:
        for index, value in cases:
            arguments = ["1", "42", "3", "none"]
            arguments[index] = value
            command = [str(binary), temporary, *arguments]
            process = subprocess.run(command, capture_output=True, text=True, timeout=3, env=lab.build_environment())
            output = process.stdout + process.stderr
            if process.returncode != 1 or "RTL_FAILURE" not in output or "RTL_LISTENING" in output or list(Path(temporary).iterdir()):
                raise RuntimeError(f"Simulator did not reject arguments before socket creation: {arguments}")
            results.append(dict(arguments=arguments, returncode=process.returncode, output=output))
    lab.write_json(evidence / "cli-rejections.json", results)
    return len(results)


def test(timeout):
    compat = build_compat()
    evidence = lab.ARTIFACTS / datetime.datetime.now(datetime.timezone.utc).strftime("hardening-%Y%m%dT%H%M%S-%fZ")
    evidence.mkdir()
    result = dict(schema=1, test="control-and-compat-hardening", status="FAIL", time_utc=lab.timestamp(),
                  hardware_tested=False, control=[], compat=[])
    started = time.monotonic()
    try:
        images = lab.ARTIFACTS / "build/guest/images"
        binaries = [compat, lab.ARTIFACTS / "build/framefx-socket/Vframefx_core",
                    lab.ARTIFACTS / "build/qemu/qemu-system-aarch64", images / "Image", images / "rootfs.cpio.gz"]
        lab.write_json(evidence / "versions.json", dict(lock=lab.LOCK, project_sha256=lab.project_hashes(),
                       binary_sha256={str(binary.relative_to(lab.ROOT)): lab.sha256(binary) for binary in binaries},
                       compat_tools={tool: lab.capture([shutil.which(tool, path=lab.build_environment()["PATH"]), "--version"])
                                     for tool in ("clang", "ld.lld")}))
        configuration = lab.ARTIFACTS / f"build/guest/build/linux-{lab.LOCK['guest']['linux']}/.config"
        shutil.copy2(configuration, evidence / "linux.config")
        shutil.copy2(lab.ARTIFACTS / "build/guest/.config", evidence / "buildroot.config")
        for option in ("COMPAT", "SMP", "PROVE_LOCKING", "DEBUG_ATOMIC_SLEEP", "DMA_API_DEBUG", "DEBUG_FS"):
            if f"CONFIG_{option}=y\n" not in configuration.read_text():
                raise RuntimeError(f"Hardening kernel missing {option}")
        result["invalid_cli_cases"] = validate_cli(evidence)
        for cpus in (1, 4):
            for name, operation, fault in CONTROL_CASES:
                result["control"].append(demo.run_case(evidence / f"{name}-{cpus}cpu", 42, 3, fault=fault,
                                                     cpus=cpus, control_case=operation, timeout=timeout))
            result["compat"].append(demo.run_case(evidence / f"compat-{cpus}cpu", 2026, 3, cpus=cpus,
                                                control_case="compat", timeout=timeout))
        result["status"] = "PASS"
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        lab.write_json(evidence / "result.json", result)
        lab.write_json(lab.ARTIFACTS / "latest-hardening.json", dict(directory=str(evidence.relative_to(lab.ROOT)), **result))
        print(f"{result['status']}: {evidence.relative_to(lab.ROOT)}", flush=True)
