import datetime
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import time

import lab


def build(jobs):
    directory = lab.ARTIFACTS / "build/spike"
    directory.mkdir(parents=True, exist_ok=True)
    lab.logged(["verilator", "--cc", "--exe", "--build", "--assert", "--trace", "--timescale", "1ns/1ps", "--Wall", "--Mdir", directory,
                "--top-module", "axi_spike", "-CFLAGS", f"-I{lab.ROOT / 'transport'} -std=c++17",
                "-j", str(jobs), lab.ROOT / "rtl/core/axi_burst_copy.sv", lab.ROOT / "rtl/spike/axi_spike.sv", lab.ROOT / "sim/spike_main.cpp"],
               lab.ARTIFACTS / "logs/spike-build.log", timeout=180)


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def validate(console, simulator, fault):
    if "reboot: Power down" not in console or any(marker in console for marker in ("Kernel panic", "BUG:", "Oops:", "WARNING: CPU:")):
        raise RuntimeError("Guest failed orderly shutdown")
    if fault != "none":
        if "AXI_COSIM_FAULT" not in console or "AXI_LAB_SPIKE_FAIL" not in console or "AXI_LAB_SPIKE_PASS" in console:
            raise RuntimeError("Fault injection did not fail closed")
        if "AXI_DMA" in console[console.index("AXI_COSIM_FAULT"):]:
            raise RuntimeError("DMA continued after a terminal bridge fault")
        return
    if not re.search(r"^AXI_LAB_SPIKE_PASS\r?$", console, re.MULTILINE):
        raise RuntimeError("Missing guest spike success marker")
    if "AXI_LAB_SPIKE_FAIL" in console or "AXI_COSIM_FAULT" in console or "RTL_FAILURE" in simulator:
        raise RuntimeError("Unexpected spike failure")
    cases = re.findall(r"SPIKE_CASE test=(\d+).* PASS", console)
    if cases != [str(index) for index in range(40)]:
        raise RuntimeError("Missing or duplicated guest cases")
    if not re.search(r"AXI_DMA kind=4 .*status=0 pending=1 bql=1", console):
        raise RuntimeError("Missing DMA progress during outstanding MMIO")
    if not re.search(r"AXI_DMA kind=[45] .*status=0 pending=0 bql=1", console):
        raise RuntimeError("Missing asynchronous DMA progress outside MMIO")
    waiting_threads = set(re.findall(r"AXI_DMA .*pending=1 bql=1 thread=(\d+)", console))
    idle_threads = set(re.findall(r"AXI_DMA .*pending=0 bql=1 thread=(\d+)", console))
    if not waiting_threads or not idle_threads or waiting_threads & idle_threads:
        raise RuntimeError("Missing distinct serialized vCPU/main-loop context evidence")
    first_dma = re.search(r"AXI_DMA kind=4 .*status=0 pending=1 bql=1", console)
    first_start = re.search(r"AXI_REPLY kind=3 .*address=0x8 error=0", console)
    if not first_start or first_dma.start() > first_start.start():
        raise RuntimeError("First DMA did not precede START response")
    if "SPIKE_RESULT result=0 tests=40 irqs=40" not in console:
        raise RuntimeError("IRQ accounting failed")
    if len(re.findall(r"AXI_DMA .*status=2 ", console)) != 4:
        raise RuntimeError("DMA range rejection checks missing")
    if "RTL_SUMMARY" not in simulator:
        raise RuntimeError("Simulator did not shut down cleanly")
    if not re.search(r"RTL_SUMMARY .*reads=61 writes=59 irqs=40 .*max_read=16 max_write=16", simulator):
        raise RuntimeError("Missing full-burst coalescing/IRQ accounting")
    if "RTL_DMA kind=4 bytes=128" not in simulator or "RTL_DMA kind=5 bytes=128" not in simulator:
        raise RuntimeError("No 16-beat DMA RPC observed")
    for index, size in enumerate((128, 136, 264, 2056), 36):
        if not re.search(rf"SPIKE_CASE test={index} .* bytes={size} .* PASS", console):
            raise RuntimeError("Missing guest multi-burst case")


def run_case(evidence, seed, fragment, fault="none", dump=False, timeout=60):
    evidence.mkdir(parents=True)
    simulator_binary = lab.ARTIFACTS / "build/spike/Vaxi_spike"
    qemu_binary = lab.ARTIFACTS / "build/qemu/qemu-system-aarch64"
    result = {"seed": seed, "fragment_bytes": fragment, "fault": fault, "status": "FAIL"}
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="axi-spike-") as temporary:
            runtime = Path(temporary)
            session = secrets.randbits(63) or 1
            simulator_command = [str(simulator_binary), str(runtime), str(session), str(seed), str(fragment), fault]
            command = lab.boot_command(qemu_binary, lab.ARTIFACTS / "build/guest/images", smoke=False)
            command[-1] += " axi_lab.spike=1"
            command.extend(["-device", f"axi-cosim,command={runtime}/command.sock,reverse={runtime}/reverse.sock,session={session}"])
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
                    if fault == "none" and simulator_status:
                        raise RuntimeError(f"Simulator exited {simulator_status}")
                finally:
                    stop(simulator)
            if dump:
                lab.logged([lab.ARTIFACTS / "build/guest/host/bin/dtc", "-I", "dtb", "-O", "dts",
                            "-o", evidence / "virt.dts", evidence / "virt.dtb"], evidence / "dtc.log", timeout=10)
                device_tree = (evidence / "virt.dts").read_text()
                for required in ('compatible = "example,axi-framefx-1.0"', "dma-coherent;", "example,concurrency-spike;", "dma-ranges = <0x40000000 0x00 0x40000000 0x20000000>;"):
                    if required not in device_tree:
                        raise RuntimeError(f"Missing DT property: {required}")
            else:
                validate((evidence / "qemu-console.log").read_text(errors="replace"),
                         (evidence / "simulator.log").read_text(errors="replace"), fault)
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
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("spike-%Y%m%dT%H%M%S-%fZ")
    evidence = lab.ARTIFACTS / run_id
    evidence.mkdir()
    result = {"schema": 1, "test": "M1-concurrency-spike", "status": "FAIL", "time_utc": lab.timestamp(),
              "accelerator_tested": False, "hardware_tested": False, "cases": []}
    images = lab.ARTIFACTS / "build/guest/images"
    binaries = [lab.ARTIFACTS / "build/spike/Vaxi_spike", lab.ARTIFACTS / "build/qemu/qemu-system-aarch64",
                images / "Image", images / "rootfs.cpio.gz"]
    try:
        lab.write_json(evidence / "versions.json", {"lock": lab.LOCK, "project_sha256": lab.project_hashes(),
                       "binary_sha256": {str(path.relative_to(lab.ROOT)): lab.sha256(path) for path in binaries}})
        shutil.copy2(lab.ARTIFACTS / "build/guest/.config", evidence / "buildroot.config")
        shutil.copy2(lab.ARTIFACTS / f"build/guest/build/linux-{lab.LOCK['guest']['linux']}/.config", evidence / "linux.config")
        import bridge_probe
        result["cases"].append(bridge_probe.run(evidence / "bridge-safety"))
        result["cases"].append(run_case(evidence / "dt", 1, 0, dump=True, timeout=timeout))
        for seed, fragment in ((1, 0), (42, 7), (2026, 1)):
            result["cases"].append(run_case(evidence / f"seed-{seed}", seed, fragment, timeout=timeout))
        for fault in ("disconnect", "stall", "malformed", "partial"):
            result["cases"].append(run_case(evidence / fault, 7, 3, fault, timeout=timeout))
        result["status"] = "PASS"
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        lab.write_json(evidence / "result.json", result)
        lab.write_json(lab.ARTIFACTS / "latest-spike.json", {"directory": str(evidence.relative_to(lab.ROOT)), **result})
