import datetime
import gzip
import json
import random
import re
import shutil
import time

import demo
import framefx
import lab
import protocol_stress


BUFFER_BYTES = 128 * 1024


def linux_cases(count, seed):
    state = seed

    def next_value():
        nonlocal state
        state ^= (state << 13) & 0xffffffff
        state ^= state >> 17
        state ^= (state << 5) & 0xffffffff
        return state

    edges = (1, 2, 3, 7, 8, 9, 63, 64, 65, 127, 128, 129, 255, 256, 257, 511, 512, 513, 1023, 1024)
    for index in range(count):
        width = 1 + next_value() % 1024 if index % 2 else edges[next_value() % len(edges)]
        height, mode = 1 + next_value() % 65, index % 4
        if mode == 3:
            width, height = max(3, width), max(3, height)
        yield dict(index=index, width=width, height=height, mode=mode,
                   source_stride=((width + 63) & ~63) + 64 * (next_value() % 9),
                   destination_stride=((width + 63) & ~63) + 64 * (next_value() % 9),
                   threshold=next_value() & 255, seed=next_value())


def random_cases(count, seed):
    if not 1 <= count <= 100000 or not 1 <= seed <= 0xffffffff:
        raise ValueError("Stress count/seed outside supported range")
    generator = random.Random(seed)
    edges = (1, 2, 3, 7, 8, 9, 63, 64, 65, 127, 128, 129, 255, 256, 257, 511, 512, 513, 1023, 1024)
    plans = []
    for index in range(count):
        maximum = (1, 4, 16)[index % 3]
        width = generator.choice(edges) if index % 2 else generator.randint(1, 1024)
        height, mode = generator.randint(1, 33), index % 4
        if mode == 3:
            width, height = max(3, width), max(3, height)
        case = dict(id=index, name=f"random-{index}", width=width, height=height, mode=mode,
                    threshold=generator.randrange(256), pattern=generator.randrange(7), scenario=0,
                    error=0, fail_read=0, fail_write=0, source_stride=((width + 63) & ~63) + 64 * generator.randrange(9),
                    destination_stride=((width + 63) & ~63) + 64 * generator.randrange(9),
                    source_offset=64 * generator.randint(1, 64), destination_page_offset=64 * generator.randrange(64))
        vector_seed = generator.getrandbits(32)
        if index % 5 in (1, 2):
            direction = "read" if index % 5 == 1 else "write"
            address = framefx.BASE + case["source_offset"] if direction == "read" else (
                framefx.BASE + ((case["source_offset"] + case["source_stride"] * height + 8191) // 4096) * 4096 + case["destination_page_offset"])
            stride = case["source_stride" if direction == "read" else "destination_stride"]
            total = sum(len(list(framefx.row_bursts(address + row * stride, width, maximum))) for row in range(height))
            case.update(error=2 if direction == "read" else 3, **{f"fail_{direction}": generator.randint(1, total)})
        plans.append(dict(case=case, seed=vector_seed, maximum=maximum))
    return plans


def validate_linux(export, console, jobs, seed, cpus):
    if type(jobs) is not int or not 1 <= jobs <= 100000:
        raise RuntimeError("Incorrect stress job budget")
    required = (f"FRAMEFX_STRESS_PASS jobs={jobs} seed={seed} cpus={cpus} workers=4 operations={14 * jobs} owners={(jobs + 31) // 32}",
                "FRAMEFX_KILLED_OWNER_PASS", "FRAMEFX_KERNEL_DEBUG_PASS", "FRAMEFX_CMA_PASS", "FRAMEFX_ACTIVE_LIFETIME_PASS")
    if not all(marker in console for marker in required):
        raise RuntimeError("Missing SMP/lifecycle/debug stress evidence")
    if any(marker in console for marker in ("AXI_COSIM_FAULT", "FRAMEFX_QUARANTINE", "FRAMEFX_STRESS_FAIL")):
        raise RuntimeError("Unexpected stress failure/quarantine")
    cma = re.search(r"FRAMEFX_CMA allocated_pages=(\d+) released_pages=(\d+)", console)
    if not cma or int(cma[1]) <= 0 or cma[1] != cma[2]:
        raise RuntimeError("Stress CMA accounting mismatch")
    if not re.search(r"debug_locks:\s+1\b", (export / "lockdep.txt").read_text()):
        raise RuntimeError("Lock dependency checker disabled")
    records = []
    with (export / "stress.jsonl").open("rb") as metadata:
        while encoded := metadata.readline(4097):
            if len(records) >= jobs:
                raise RuntimeError("Incorrect stress image count")
            if len(encoded) > 4096:
                raise RuntimeError("Oversized stress metadata")
            try:
                record = json.loads(encoded)
            except (ValueError, UnicodeError) as error:
                raise RuntimeError("Invalid stress metadata") from error
            if not isinstance(record, dict):
                raise RuntimeError("Invalid stress metadata object")
            records.append(record)
    if len(records) != jobs:
        raise RuntimeError("Incorrect stress image count")
    previous_job = previous_irq = 0
    raw = export / "destination.bin"
    opener = raw.open("rb") if raw.exists() else gzip.open(raw.with_suffix(".bin.gz"), "rb")
    with opener as stream:
        for index, (record, plan) in enumerate(zip(records, linux_cases(jobs, seed))):
            if any(type(record.get(key)) is not int or record[key] != value for key, value in plan.items()):
                raise RuntimeError("Stress image manifest differs from seeded plan")
            try:
                demo.validate_record(dict(record, source_bytes=BUFFER_BYTES, destination_bytes=BUFFER_BYTES, status="PASS"))
            except RuntimeError as error:
                raise RuntimeError("Incorrect stress completion metadata") from error
            width, height, mode = record["width"], record["height"], record["mode"]
            stride, source_stride = record["destination_stride"], record["source_stride"]
            if (record["index"] != index or mode != index % 4 or not 1 <= width <= 1024 or not 1 <= height <= 65 or
                    not 0 <= record["threshold"] <= 255 or (mode == 3 and min(width, height) < 3) or
                    any(value % 64 or value < width or value * height > BUFFER_BYTES for value in (stride, source_stride))):
                raise RuntimeError("Incorrect randomized geometry/coverage")
            pixels = framefx.image(width, height, 0, record["seed"], record["threshold"])
            transformed = framefx.transform(pixels, width, height, mode, record["threshold"])
            expected = bytearray([0x5a]) * BUFFER_BYTES
            for row in range(height):
                expected[row * stride:row * stride + width] = transformed[row * width:(row + 1) * width]
            if stream.read(BUFFER_BYTES) != expected:
                raise RuntimeError(f"Independent stress full-buffer comparison failed: job {index}")
            if (record["bytes_read"] != ((width + 7) & ~7) * height or record["bytes_written"] != width * height or
                    record["cycles"] <= 0 or record["job_id"] <= 0 or record["irq_count"] <= 0 or
                    (index and (record["job_id"] != previous_job + 1 or record["irq_count"] != previous_irq + 1))):
                raise RuntimeError(f"Incorrect stress completion accounting: job {index}")
            previous_job, previous_irq = record["job_id"], record["irq_count"]
        if stream.read(1):
            raise RuntimeError("Incorrect stress snapshot length")
    return dict(images=jobs, allocated_pages=int(cma[1]), released_pages=int(cma[2]),
                widths=len({record["width"] for record in records}), thresholds=len({record["threshold"] for record in records}),
                sha256={str(path.relative_to(export)): lab.sha256(path) for path in sorted(export.iterdir()) if path.is_file()})


def sanitize(evidence):
    evidence.mkdir(parents=True)
    binary = evidence / "fabric-sanitized"
    lab.logged(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", "-g",
                "-fno-omit-frame-pointer", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                "-I", lab.ROOT / "sim", lab.ROOT / "tests/fabric_unit.cpp", "-o", binary], evidence / "build.log", timeout=120)
    lab.logged([binary], evidence / "run.log", timeout=30)
    if "MONITOR_UNIT_PASS negative_cases=14" not in (evidence / "run.log").read_text():
        raise RuntimeError("Missing sanitizer completion marker")
    return binary


def test(jobs, cases, linux_jobs, seed, timeout):
    plans = random_cases(cases, seed)
    if not 1 <= linux_jobs <= 100000:
        raise ValueError("Linux stress jobs outside supported range")
    binaries = {maximum: framefx.build(jobs, maximum) for maximum in (1, 4, 16)}
    evidence = lab.ARTIFACTS / datetime.datetime.now(datetime.timezone.utc).strftime("stress-%Y%m%dT%H%M%S-%fZ")
    evidence.mkdir()
    result = dict(schema=1, test="platform-stress", status="FAIL", time_utc=lab.timestamp(), hardware_tested=False,
                  seed=seed, standalone_cases=0, requested_cases=cases, linux_jobs_per_boot=linux_jobs, linux=[], protocol=[])
    started = time.monotonic()
    try:
        images = lab.ARTIFACTS / "build/guest/images"
        tested = [*binaries.values(), lab.ARTIFACTS / "build/framefx-socket/Vframefx_core",
                  lab.ARTIFACTS / "build/qemu/qemu-system-aarch64", images / "Image", images / "rootfs.cpio.gz"]
        lab.write_json(evidence / "versions.json", dict(lock=lab.LOCK, project_sha256=lab.project_hashes(),
                       binary_sha256={str(binary.relative_to(lab.ROOT)): lab.sha256(binary) for binary in tested}))
        configuration = lab.ARTIFACTS / f"build/guest/build/linux-{lab.LOCK['guest']['linux']}/.config"
        shutil.copy2(configuration, evidence / "linux.config")
        shutil.copy2(lab.ARTIFACTS / "build/guest/.config", evidence / "buildroot.config")
        for option in ("SMP", "PROVE_LOCKING", "DEBUG_ATOMIC_SLEEP", "DMA_API_DEBUG", "DEBUG_FS"):
            if f"CONFIG_{option}=y\n" not in configuration.read_text():
                raise RuntimeError(f"Stress kernel missing {option}")
        lab.write_json(evidence / "plan.json", plans)
        sanitizer = sanitize(evidence / "sanitizers")
        result["sanitizers"] = dict(status="PASS", binary_sha256=lab.sha256(sanitizer))
        result["protocol"] = protocol_stress.test(evidence / "protocol")
        for plan in plans:
            case, vector_seed, maximum = plan["case"], plan["seed"], plan["maximum"]
            directory = evidence / f"standalone/case-{case['id']}"
            expected = framefx.run_case(directory, binaries[maximum], case, vector_seed, maximum)
            result["standalone_cases"] += 1
            if case["id"] < 3:
                replay = evidence / f"replay/case-{case['id']}"
                observed = framefx.run_case(replay, binaries[maximum], case, vector_seed, maximum, tracing=True)
                if expected != observed or (directory / "events.log").read_bytes() != (replay / "events.log").read_bytes():
                    raise RuntimeError("Randomized traced replay diverged")
        for cpus, fragment in ((1, 0), (4, 3)):
            directory = evidence / f"linux-{cpus}cpu"
            result["linux"].append(demo.run_case(directory, seed, fragment, timeout=timeout, cpus=cpus, stress_jobs=linux_jobs))
            raw = directory / "export/destination.bin"
            with raw.open("rb") as source, gzip.open(raw.with_suffix(".bin.gz"), "wb", compresslevel=6) as destination:
                shutil.copyfileobj(source, destination)
            raw.unlink()
            result["linux"][-1]["compressed_snapshot"] = dict(file=str(raw.with_suffix(".bin.gz").relative_to(evidence)),
                                                             sha256=lab.sha256(raw.with_suffix(".bin.gz")))
            lab.write_json(directory / "result.json", result["linux"][-1])
        result.update(status="PASS", traced_replays=min(3, cases),
                      coverage=dict(modes=sorted({plan["case"]["mode"] for plan in plans}),
                                    width_modulo_8=sorted({plan["case"]["width"] % 8 for plan in plans}),
                                    patterns=sorted({plan["case"]["pattern"] for plan in plans}),
                                    parameter_limits=sorted({plan["maximum"] for plan in plans}),
                                    read_errors=sum(plan["case"]["error"] == 2 for plan in plans),
                                    write_errors=sum(plan["case"]["error"] == 3 for plan in plans)))
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        lab.write_json(evidence / "result.json", result)
        lab.write_json(lab.ARTIFACTS / "latest-stress.json", dict(directory=str(evidence.relative_to(lab.ROOT)), **result))
        print(f"{result['status']}: {evidence.relative_to(lab.ROOT)}", flush=True)
