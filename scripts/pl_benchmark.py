import argparse
import datetime
import json
import math
from pathlib import Path
import statistics
import re
import zlib

import demo
import framefx
import lab


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def integer(record, key, minimum, maximum):
    value = record.get(key)
    require(type(value) is int and minimum <= value <= maximum, "Invalid benchmark field: " + key)
    return value


def distribution(values):
    ordered = sorted(values)
    require(bool(ordered) and all(type(value) in (int, float) and math.isfinite(value) and value > 0
                                 for value in ordered), "Invalid timing distribution")
    return dict(count=len(ordered), minimum=ordered[0], median=statistics.median(ordered),
                p95=ordered[math.ceil(len(ordered) * 0.95) - 1],
                p99=ordered[math.ceil(len(ordered) * 0.99) - 1], maximum=ordered[-1],
                mean=statistics.mean(ordered))


def reference_buffers(plan, seed):
    pixels = framefx.image(plan["width"], plan["height"], 0, seed, plan["threshold"])
    output = framefx.transform(pixels, plan["width"], plan["height"], plan["mode"], plan["threshold"])
    result = {}
    for kind, data, fill in (("source", pixels, 0xa5), ("destination", output, 0x5a)):
        buffer = bytearray([fill]) * plan[kind + "_bytes"]
        for row in range(plan["height"]):
            start = row * plan[kind + "_stride"]
            buffer[start:start + plan["width"]] = data[row * plan["width"]:(row + 1) * plan["width"]]
        result[kind] = buffer
    return result


def validate(directory, expected_plan=None, clock_hz=None):
    require(directory.is_dir() and not directory.is_symlink(), "Unsafe benchmark directory")
    paths = [directory / name for name in ("benchmark.jsonl", "source.bin", "destination.bin")]
    require(all(path.is_file() and not path.is_symlink() for path in paths), "Missing/unsafe benchmark evidence")
    records = []
    with paths[0].open("rb") as stream:
        for index in range(275):
            line = stream.readline(4097)
            if not line:
                break
            require(index < 274 and len(line) <= 4096 and line.endswith(b"\n"), "Oversized/truncated benchmark evidence")
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError) as error:
                raise RuntimeError("Invalid benchmark JSON") from error
            require(type(record) is dict, "Invalid benchmark record")
            records.append(record)
    require(len(records) >= 3, "Incomplete benchmark")
    plan, result = records[0], records[-1]
    require(plan.get("type") == "plan" and type(plan.get("schema")) is int and plan["schema"] == 1,
            "Invalid benchmark plan")
    for key, minimum, maximum in (("width", 3, 1024), ("height", 3, 1024), ("mode", 0, 3),
                                   ("samples", 1, 256), ("warmup", 0, 16), ("contend", 0, 1),
                                   ("threshold", 127, 127), ("seed_base", 12000, 12000)):
        integer(plan, key, minimum, maximum)
    if expected_plan is not None:
        require(all(plan.get(key) == value and type(plan.get(key)) is type(value)
                    for key, value in expected_plan.items()), "Unexpected benchmark coverage")
    for kind, padding in (("source", 64), ("destination", 128)):
        stride = ((plan["width"] + 63) & ~63) + padding
        size = (stride * plan["height"] + 8191) & ~4095
        integer(plan, kind + "_stride", stride, stride)
        integer(plan, kind + "_bytes", size, size)
    require(plan.get("clock") == "CLOCK_MONOTONIC_RAW", "Unexpected timing clock")
    integer(plan, "allocation_ns", 1, 60 * 10**9)
    caps = plan.get("caps_ns")
    require(type(caps) is list and len(caps) == 128 and
            all(type(value) is int and 0 < value <= 10**9 for value in caps), "Invalid capability timings")
    count = plan["samples"] + plan["warmup"]
    require(len(records) == count + 2, "Incorrect sample count")
    require(result.get("type") == "result" and result.get("status") == "PASS" and
            result.get("ownership_released") is True, "Missing benchmark completion")
    integer(result, "jobs", count, count)
    queries = integer(result, "control_queries", 0, 10**8)
    control_total = integer(result, "control_total_ns", 0, 3600 * 10**9)
    control_max = integer(result, "control_max_ns", 0, 60 * 10**9)
    require((plan["contend"] == 0 and queries == control_total == control_max == 0) or
            (plan["contend"] == 1 and queries > 0 and 0 < control_max <= control_total <= queries * control_max),
            "Invalid concurrent control accounting")
    samples = records[1:-1]
    checksums = {}
    for index, sample in enumerate(samples):
        require(sample.get("type") == "sample" and sample.get("status") == "PASS", "Unverified sample")
        integer(sample, "index", index, index)
        seed = plan["seed_base"] + index % 4
        integer(sample, "seed", seed, seed)
        for key in ("job_id", "irq_count", "cycles"):
            integer(sample, key, 1, 2**64 - 1)
        for key in ("prepare_ns", "submit_ns", "wait_ns", "elapsed_ns", "verify_ns"):
            integer(sample, key, 1, 60 * 10**9)
        require(sample["elapsed_ns"] == sample["submit_ns"] + sample["wait_ns"], "Inconsistent timing interval")
        integer(sample, "bytes_read", ((plan["width"] + 7) & ~7) * plan["height"],
                ((plan["width"] + 7) & ~7) * plan["height"])
        integer(sample, "bytes_written", plan["width"] * plan["height"], plan["width"] * plan["height"])
        if index:
            require(all(sample[key] == samples[index - 1][key] + 1 for key in ("job_id", "irq_count")),
                    "Nonsequential completion counters")
        if seed not in checksums:
            checksums[seed] = {kind: zlib.crc32(buffer) for kind, buffer in reference_buffers(plan, seed).items()}
        for kind in ("source", "destination"):
            crc = integer(sample, kind + "_crc32", 0, 2**32 - 1)
            require(crc == checksums[seed][kind], "Independent full-buffer CRC mismatch: " + kind)
    for kind, buffer in reference_buffers(plan, samples[-1]["seed"]).items():
        require(demo.matches_file(directory / (kind + ".bin"), buffer), "Independent final snapshot mismatch: " + kind)
    measured = samples[plan["warmup"]:]
    timings = {key: distribution([sample[key] for sample in measured])
               for key in ("prepare_ns", "submit_ns", "wait_ns", "elapsed_ns", "verify_ns", "cycles")}
    elapsed = sum(sample["elapsed_ns"] for sample in measured) / 10**9
    payload = sum(sample["bytes_written"] for sample in measured)
    traffic = sum(sample["bytes_read"] + sample["bytes_written"] for sample in measured)
    summary = dict(status="PASS", plan=plan, jobs=count, measured_jobs=len(measured), timings=timings,
                   caps_ns=distribution(caps), payload_mib_per_second=payload / elapsed / 2**20,
                   counted_dma_mib_per_second=traffic / elapsed / 2**20,
                   jobs_per_active_second=len(measured) / elapsed,
                   irq_first=samples[0]["irq_count"], irq_last=samples[-1]["irq_count"], control=result,
                   artifact_sha256={path.name: lab.sha256(path) for path in paths})
    if clock_hz is not None:
        require(type(clock_hz) is int and 1 <= clock_hz <= 10**9, "Invalid nominal PL frequency")
        hardware_seconds = sum(sample["cycles"] for sample in measured) / clock_hz
        summary.update(nominal_pl_clock_hz=clock_hz, hardware_payload_mib_per_second=payload / hardware_seconds / 2**20,
                       hardware_ns=distribution([sample["cycles"] * 10**9 / clock_hz for sample in measured]))
    return summary


def validate_simulation(export, console, simulator):
    require("FRAMEFX_BENCH_KERNEL_PASS" in console and "AXI_COSIM_FAULT" not in console and
            "FRAMEFX_QUARANTINE" not in console, "Missing benchmark kernel health")
    require(re.search(r"RTL_SUMMARY .* irqs=48 ", simulator) and "RTL_FAILURE" not in simulator,
            "Incorrect simulator completion count")
    profiles = []
    for mode in range(4):
        for contend in range(2):
            profile = validate(export / f"mode-{mode}-control-{contend}",
                               dict(width=65, height=9, mode=mode, samples=4, warmup=2, contend=contend))
            require(profile["irq_first"] == len(profiles) * 6 + 1 and profile["irq_last"] == (len(profiles) + 1) * 6,
                    "Lost simulator IRQ continuity")
            profiles.append(profile)
    return dict(status="PASS", jobs=48, profiles=profiles, physical_performance="NOT_MEASURED")


def simulation(timeout):
    directory = lab.ARTIFACTS / datetime.datetime.now(datetime.timezone.utc).strftime("benchmark-sim-%Y%m%dT%H%M%S-%fZ")
    directory.mkdir()
    record = dict(status="FAIL", directory=str(directory.relative_to(lab.ROOT)), hardware_tested=False,
                  project_sha256=lab.project_hashes(), cases=[])
    binaries = ("build/qemu/qemu-system-aarch64", "build/framefx-socket/Vframefx_core",
                "build/guest/images/Image", "build/guest/images/rootfs.cpio.gz")
    record["binary_sha256"] = {str((lab.ARTIFACTS / name).relative_to(lab.ROOT)): lab.sha256(lab.ARTIFACTS / name)
                               for name in binaries}
    lab.write_json(directory / "versions.json", {key: record[key] for key in ("project_sha256", "binary_sha256")})
    try:
        for cpus in (1, 4):
            record["cases"].append(demo.run_case(directory / f"cpus-{cpus}", 2026, 3, timeout=timeout,
                                                  cpus=cpus, benchmark=True))
        record["status"] = "PASS"
    finally:
        lab.write_json(directory / "result.json", record)
        lab.write_json(lab.ARTIFACTS / "latest-benchmark-sim.json", record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline PS/PL benchmark validation; never connects to hardware")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--clock-hz", type=int, help="Nominal PL clock from the verified platform, not inferred from host timing")
    arguments = parser.parse_args()
    print(json.dumps(validate(arguments.directory, clock_hz=arguments.clock_hz), indent=2))
