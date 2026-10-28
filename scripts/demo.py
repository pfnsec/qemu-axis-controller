import datetime
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time

import framefx
import lab
import spike


def build(jobs):
    directory = lab.ARTIFACTS / "build/framefx-socket"
    directory.mkdir(parents=True, exist_ok=True)
    lab.logged(["verilator", "--cc", "--exe", "--build", "--assert", "--trace", "--timescale", "1ns/1ps", "--Wall",
                "--Mdir", directory, "--top-module", "framefx_core", "-CFLAGS",
                f"-I{lab.ROOT / 'transport'} -std=c++17 -DAXI_FRAMEFX", "-j", str(jobs),
                *framefx.SOURCES, lab.ROOT / "sim/spike_main.cpp"], lab.ARTIFACTS / "logs/framefx-socket-build.log", timeout=180)


def validate_image(filename):
    with filename.open("rb") as stream:
        encoded = stream.read(4097)
    if len(encoded) > 4096:
        raise RuntimeError(f"Oversized image metadata: {filename}")
    try:
        record = json.loads(encoded)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(f"Invalid image metadata: {filename}") from error
    validate_record(record)
    index = filename.stem.split("-")[1]
    width, height = record["width"], record["height"]
    pixels = framefx.image(width, height, 0, record["seed"], record["threshold"])
    expected = framefx.transform(pixels, width, height, record["mode"], record["threshold"])
    header = f"P5\n{width} {height}\n255\n".encode()
    for kind, image in (("input", pixels), ("output", expected)):
        if not matches_file(filename.parent / f"{kind}-{index}.pgm", header + image):
            raise RuntimeError(f"Independent PGM comparison failed: {filename} {kind}")
    for kind, image, fill in (("source", pixels, 0xa5), ("destination", expected, 0x5a)):
        stride = record[f"{kind}_stride"]
        buffer = bytearray([fill]) * record[f"{kind}_bytes"]
        for row in range(height):
            buffer[row * stride:row * stride + width] = image[row * width:(row + 1) * width]
        if not matches_file(filename.parent / f"{kind}-{index}.bin", buffer):
            raise RuntimeError(f"Independent full-allocation comparison failed: {filename} {kind}")
    return record


def validate_images(export):
    records = []
    filenames = sorted(export.glob("*/job-*.json"))
    if len(filenames) > 28:
        raise RuntimeError(f"Expected 28 images, found {len(filenames)}")
    for filename in filenames:
        records.append({**validate_image(filename), "file": str(filename.relative_to(export))})
    if len(records) != 28:
        raise RuntimeError(f"Expected 28 images, found {len(records)}")
    profiles = {"small": (320, 240, 1, 4), "medium": (640, 480, 42, 4),
                "narrow": (65, 9, 2026, 12), "maximum": (1024, 1024, 7, 4), "reload": (3, 3, 1, 4)}
    expected_files = {f"{profile}/job-{index}.json" for profile, (_, _, _, count) in profiles.items() for index in range(count)}
    if {record["file"] for record in records} != expected_files:
        raise RuntimeError("Incorrect image manifest")
    for record in records:
        profile, basename = record["file"].split("/")
        index = int(basename.removeprefix("job-").removesuffix(".json"))
        width, height, seed, _ = profiles[profile]
        if (record["width"], record["height"], record["seed"], record["mode"], record["threshold"]) != (width, height, seed + index, index % 4, 127):
            raise RuntimeError("Incorrect image coverage")
        if record["cycles"] <= 0 or record["irq_count"] <= 0 or record["job_id"] <= 0:
            raise RuntimeError("Missing live hardware counters")
    return {"images": records, "sha256": {str(path.relative_to(export)): lab.sha256(path)
            for path in sorted(export.rglob("*")) if path.is_file()}}


def matches_file(filename, expected):
    with filename.open("rb") as stream:
        return stream.read(len(expected) + 1) == expected


def validate_record(record):
    limits = dict(width=(1, 1024), height=(1, 1024), seed=(0, 0xffffffff), mode=(0, 3), threshold=(0, 255),
                  source_stride=(64, 2**21), destination_stride=(64, 2**21),
                  source_bytes=(4096, 2**21), destination_bytes=(4096, 2**21),
                  job_id=(1, 2**64 - 1), cycles=(1, 2**64 - 1), irq_count=(1, 2**64 - 1),
                  bytes_read=(0, 2**32 - 1), bytes_written=(0, 2**32 - 1))
    if not isinstance(record, dict) or any(type(record.get(key)) is not int or not low <= record[key] <= high
                                          for key, (low, high) in limits.items()):
        raise RuntimeError("Invalid image metadata types/ranges")
    width, height = record["width"], record["height"]
    if record["mode"] == 3 and min(width, height) < 3:
        raise RuntimeError("Invalid image geometry")
    for kind in ("source", "destination"):
        stride, size = record[f"{kind}_stride"], record[f"{kind}_bytes"]
        if stride % 64 or stride < width or size % 4096 or stride * height > size:
            raise RuntimeError("Invalid image buffer geometry")
    if record.get("status") != "PASS" or record["bytes_read"] != ((width + 7) & ~7) * height or record["bytes_written"] != width * height:
        raise RuntimeError("Incorrect counters")


def run_case(evidence, seed, fragment, fault="none", timeout=180, dump=False, cpus=1, stress_jobs=0, control_case=None,
             benchmark=False):
    evidence.mkdir(parents=True)
    export = evidence / "export"
    export.mkdir()
    if control_case == "compat":
        shutil.copy2(lab.ARTIFACTS / "build/compat32/framefx-compat32", export / "framefx-compat32")
    result = {"seed": seed, "fragment_bytes": fragment, "fault": fault, "cpus": cpus, "status": "FAIL"}
    dma_error = fault.startswith(("read-error:", "write-error:"))
    terminal_fault = fault != "none" and not dma_error and not fault.startswith("mmio-corrupt:")
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="framefx-") as temporary:
            runtime = Path(temporary)
            session = secrets.randbits(63) or 1
            simulator_command = [str(lab.ARTIFACTS / "build/framefx-socket/Vframefx_core"), str(runtime),
                                 str(session), str(seed), str(fragment), fault]
            command = lab.boot_command(lab.ARTIFACTS / "build/qemu/qemu-system-aarch64",
                                       lab.ARTIFACTS / "build/guest/images", smoke=False)
            command[command.index("-serial") + 1] = f"file:{evidence / 'guest-console.log'}"
            command[command.index("-smp") + 1] = str(cpus)
            if cpus > 1:
                command[command.index("-accel") + 1] = "tcg,thread=multi"
            command[-1] += (" axi_lab.benchmark=1" if benchmark else
                            f" axi_lab.control={control_case} axi_lab.terminal={int(terminal_fault)}" if control_case else
                            f" axi_lab.stress={stress_jobs} axi_lab.seed={seed}" if stress_jobs else
                            " axi_lab.framefx=1" + (" axi_lab.fault=1" if terminal_fault else ""))
            if dma_error:
                command[-1] += " axi_lab.axi_error=" + ("2" if fault.startswith("read-error:") else "3")
            command.extend(["-device", f"axi-cosim,command={runtime}/command.sock,reverse={runtime}/reverse.sock,session={session},spike=off",
                            "-fsdev", f"local,id=evidence,path={export},security_model=none",
                            "-device", "virtio-9p-device,fsdev=evidence,mount_tag=evidence"])
            if dump:
                command[2] += f",dumpdtb={evidence / 'virt.dtb'}"
            lab.write_json(evidence / "commands.json", {"qemu": command, "simulator": simulator_command})
            with (evidence / "simulator.log").open("w") as output:
                simulator = subprocess.Popen(simulator_command, stdout=output, stderr=subprocess.STDOUT,
                                             env=lab.build_environment(), start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while "RTL_LISTENING" not in (evidence / "simulator.log").read_text():
                        if simulator.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Simulator failed to start")
                        time.sleep(0.01)
                    lab.logged(command, evidence / "qemu-console.log", timeout=timeout)
                    simulator_status = simulator.wait(timeout=8)
                    if not terminal_fault and simulator_status:
                        raise RuntimeError(f"Simulator exited {simulator_status}")
                finally:
                    spike.stop(simulator)
            console = (evidence / "guest-console.log").read_text(errors="replace") + "\n" + (evidence / "qemu-console.log").read_text(errors="replace")
            if dump:
                lab.logged([lab.ARTIFACTS / "build/guest/host/bin/dtc", "-I", "dtb", "-O", "dts", "-o",
                            evidence / "virt.dts", evidence / "virt.dtb"], evidence / "dtc.log", timeout=10)
                tree = (evidence / "virt.dts").read_text()
                if 'compatible = "example,axi-framefx-1.0"' not in tree or "example,concurrency-spike" in tree:
                    raise RuntimeError("Incorrect production device tree")
            else:
                if "AXI_LAB_FRAMEFX_PASS" not in console or "AXI_LAB_FRAMEFX_FAIL" in console or "reboot: Power down" not in console:
                    raise RuntimeError("Guest did not pass and shut down")
                if any(marker in console for marker in ("Kernel panic", "BUG:", "Oops:", "WARNING: CPU:",
                                                        "possible circular locking", "inconsistent lock state", "DMA-API: debugging out of memory")):
                    raise RuntimeError("Kernel diagnostic detected")
                if benchmark:
                    import pl_benchmark
                    result["validation"] = pl_benchmark.validate_simulation(export, console,
                                                                           (evidence / "simulator.log").read_text())
                elif control_case:
                    import hardening
                    simulator_log = (evidence / "simulator.log").read_text()
                    result["validation"] = (hardening.validate_compat(console, simulator_log) if control_case == "compat" else
                                            hardening.validate_control(console, simulator_log, control_case, terminal_fault))
                elif stress_jobs:
                    import stress
                    result["validation"] = stress.validate_linux(export, console, stress_jobs, seed, cpus)
                    simulator_log = (evidence / "simulator.log").read_text()
                    if "RTL_SUMMARY" not in simulator_log or "RTL_FAILURE" in simulator_log:
                        raise RuntimeError("Stress simulator did not complete cleanly")
                elif terminal_fault:
                    if not all(marker in console for marker in ("FRAMEFX_FAULT_PASS", "FRAMEFX_QUARANTINE", "AXI_COSIM_FAULT")):
                        raise RuntimeError("Missing fail-closed/quarantine evidence")
                    if "AXI_DMA" in console[console.index("AXI_COSIM_FAULT"):]:
                        raise RuntimeError("DMA continued after terminal fault")
                    if len(re.findall(r"AXI_DMA .*status=0 ", console)) != 10 or "AXI_DMA kind=5" not in console:
                        raise RuntimeError("Fault did not occur after partial DMA progress")
                else:
                    if "AXI_COSIM_FAULT" in console or "FRAMEFX_QUARANTINE" in console:
                        raise RuntimeError("Unexpected terminal fault")
                    for marker in ("FRAMEFX_NEGATIVE_PASS", "FRAMEFX_LIFETIME_PASS", "FRAMEFX_RESULT"):
                        if console.count(marker) != 5:
                            raise RuntimeError(f"Missing regression: {marker}")
                    if "FRAMEFX_UNLOAD_RELOAD_PASS" not in console:
                        raise RuntimeError("Module reload failed")
                    if "FRAMEFX_CMA_PASS" not in console:
                        raise RuntimeError("Coherent allocations were not reclaimed")
                    simulator_log = (evidence / "simulator.log").read_text()
                    if "RTL_SUMMARY" not in simulator_log or "RTL_FAILURE" in simulator_log:
                        raise RuntimeError("Simulator did not complete cleanly")
                    if not re.search(r"RTL_SUMMARY .* irqs=29 .*max_read=16 max_write=16", simulator_log):
                        raise RuntimeError("Incorrect RTL interrupt or burst accounting")
                    if dma_error:
                        expected_fault = "error=2 read=768 written=640" if fault.startswith("read-error:") else "error=3 read=960 written=640"
                        if f"FRAMEFX_DMA_ERROR_PASS {expected_fault}" not in console or "RTL_INJECTED_AXI" not in simulator_log:
                            raise RuntimeError("Missing recoverable DMA error evidence")
                    elif "FRAMEFX_ACTIVE_LIFETIME_PASS" not in console:
                        raise RuntimeError("Missing active-close/timeout/signal evidence")
                    result["validation"] = validate_images(export)
            result["status"] = "PASS"
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        lab.write_json(evidence / "result.json", result)
        print(f"{result['status']}: {evidence.relative_to(lab.ROOT)}", flush=True)
    return result


def test(timeout):
    evidence = lab.ARTIFACTS / datetime.datetime.now(datetime.timezone.utc).strftime("demo-%Y%m%dT%H%M%S-%fZ")
    evidence.mkdir()
    result = {"schema": 1, "test": "M4-linux-framefx", "status": "FAIL", "time_utc": lab.timestamp(),
              "hardware_tested": False, "cases": []}
    images = lab.ARTIFACTS / "build/guest/images"
    binaries = [lab.ARTIFACTS / "build/framefx-socket/Vframefx_core", lab.ARTIFACTS / "build/qemu/qemu-system-aarch64",
                images / "Image", images / "rootfs.cpio.gz"]
    try:
        lab.write_json(evidence / "versions.json", {"lock": lab.LOCK, "project_sha256": lab.project_hashes(),
                       "binary_sha256": {str(path.relative_to(lab.ROOT)): lab.sha256(path) for path in binaries}})
        shutil.copy2(lab.ARTIFACTS / "build/guest/.config", evidence / "buildroot.config")
        shutil.copy2(lab.ARTIFACTS / f"build/guest/build/linux-{lab.LOCK['guest']['linux']}/.config", evidence / "linux.config")
        result["cases"].append(run_case(evidence / "dt", 1, 0, dump=True, timeout=timeout))
        for seed, fragment in ((1, 0), (42, 7)):
            result["cases"].append(run_case(evidence / f"seed-{seed}", seed, fragment, timeout=timeout))
        for fault in ("read-error:7", "write-error:7"):
            result["cases"].append(run_case(evidence / fault, 2026, 3, fault=fault, timeout=timeout))
        for fault in ("disconnect:11", "stall:11", "malformed:11", "partial:11"):
            result["cases"].append(run_case(evidence / fault, 7, 3, fault=fault, timeout=timeout))
        result["status"] = "PASS"
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        lab.write_json(evidence / "result.json", result)
        lab.write_json(lab.ARTIFACTS / "latest-demo.json", {"directory": str(evidence.relative_to(lab.ROOT)), **result})
