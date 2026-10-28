import datetime
import json
import re
import shutil
import time

import lab


BASE = 0x100000000
SOURCES = [lab.ROOT / f"rtl/core/framefx_{name}.sv" for name in ("pixel", "dma", "pipeline", "core")]


def transform(pixels, width, height, mode, threshold):
    if len(pixels) != width * height or mode not in range(4) or min(width, height) < 1:
        raise ValueError("Invalid reference image")
    if mode == 0:
        return bytes(pixels)
    if mode == 1:
        return bytes(255 - value for value in pixels)
    if mode == 2:
        return bytes(255 if value >= threshold else 0 for value in pixels)
    output = bytearray(len(pixels))
    for row in range(1, height - 1):
        above = (row - 1) * width
        current = row * width
        below = (row + 1) * width
        for column in range(1, width - 1):
            left = pixels[above + column - 1] + 2 * pixels[current + column - 1] + pixels[below + column - 1]
            right = pixels[above + column + 1] + 2 * pixels[current + column + 1] + pixels[below + column + 1]
            top = pixels[above + column - 1] + 2 * pixels[above + column] + pixels[above + column + 1]
            bottom = pixels[below + column - 1] + 2 * pixels[below + column] + pixels[below + column + 1]
            output[current + column] = min(255, (abs(right - left) + abs(bottom - top)) // 4)
    return bytes(output)


def image(width, height, pattern, seed, threshold):
    output = bytearray(width * height)
    for row in range(height):
        for column in range(width):
            if pattern == 0:
                value = ((row * 0x45d9f3b + column * 0x119de1f3 + seed * 17) ^ (column >> 3) ^ (row << 5)) & 255
            elif pattern == 1:
                value = (row * 7 + column * 3) & 255
            elif pattern == 2:
                value = 255 if (row + column) % 2 else 0
            elif pattern == 3:
                value = 255 if row == height // 2 and column == width // 2 else 0
            elif pattern == 4:
                value = (max(0, threshold - 1), threshold, min(255, threshold + 1))[(row + column) % 3]
            elif pattern == 5:
                value = 255 if column >= width // 2 and row >= height // 2 else 0
            else:
                value = 128
            output[row * width + column] = value
    return bytes(output)


def cases():
    result = []

    def add(width, height, mode, **extra):
        index = len(result)
        stride = (width + 63) & ~63
        result.append(dict(id=index, name="image", width=width, height=height, mode=mode, threshold=127,
                           source_stride=stride + (index % 3) * 64, destination_stride=stride + 64,
                           pattern=index % 7, scenario=0, error=0, fail_read=0, fail_write=0))
        result[-1].update(extra)

    for mode in range(4):
        for width in (1, 2, 3, 7, 8, 9, 63, 64, 65, 127, 320, 640, 1024):
            for height in (1, 3, 9):
                if mode != 3 or (width >= 3 and height >= 3):
                    add(width, height, mode)
        for width, height in ((320, 240), (640, 480), (1024, 1024)):
            add(width, height, mode, pattern=0)
    for mode in range(4):
        for pattern in range(7):
            add(3, 3, mode, pattern=pattern, threshold=(0, 1, 127, 128, 254, 255, 127)[pattern])
    for scenario, name, error in ((1, "busy-start", 4), (2, "consecutive-100", 0), (3, "active-reset", 0), (4, "bad-rlast", 5)):
        add(65, 9, 3, scenario=scenario, name=name, error=error)
    for mode in (0, 3):
        for channel in ("read", "write"):
            for ordinal in (1, 2, 7):
                add(257, 9, mode, name=f"{channel}-error-{ordinal}", error=2 if channel == "read" else 3,
                    **{f"fail_{channel}": ordinal})
    for name, overrides in (
        ("zero-width", {"width": 0}), ("wide", {"width": 1025}), ("zero-height", {"height": 0}),
        ("tall", {"height": 1025}), ("sobel-narrow", {"width": 2}), ("sobel-short", {"height": 2}),
        ("short-stride", {"source_stride": 0}), ("unaligned-stride", {"destination_stride": 65}),
        ("bad-mode", {"mode": 4}), ("unaligned-source", {"source": BASE + 65}),
        ("unaligned-destination", {"destination": BASE + 65}), ("overlap", {"destination": BASE + 0xf80}),
        ("source-overflow", {"source": (1 << 64) - 64}), ("destination-overflow", {"destination": (1 << 64) - 64})):
        add(65, 9, 3, name=name, error=1)
        result[-1].update(overrides)
    add(65, 9, 0, name="read-unmapped", source=BASE + 0x10000000, fail_read=1, error=2)
    add(65, 9, 0, name="write-unmapped", destination=BASE + 0x10000000, fail_write=1, error=3)
    return result


def row_bursts(address, count, maximum):
    offset = 0
    while offset < count:
        length = min(maximum * 8, ((count - offset + 7) // 8) * 8, 4096 - (address + offset) % 4096)
        yield address + offset, length // 8, min(length, count - offset)
        offset += length


def prepare(case, seed, maximum):
    width, height = case["width"], case["height"]
    source_stride, destination_stride = case["source_stride"], case["destination_stride"]
    source_offset = case.get("source_offset", 0xfc0 if case["id"] % 2 else 0xf80)
    destination_offset = ((source_offset + source_stride * height + 8191) // 4096) * 4096 + case.get("destination_page_offset", 0xfc0)
    size = destination_offset + destination_stride * height + 4096
    original = bytearray((bytes(range(256)) * ((size + 255) // 256))[:size])
    valid = case["error"] != 1
    pixels = image(width, height, case["pattern"], seed, case["threshold"]) if valid else b""
    for row in range(height if valid else 0):
        original[source_offset + row * source_stride:source_offset + row * source_stride + width] = pixels[row * width:(row + 1) * width]
    source = case.get("source", BASE + source_offset)
    destination = case.get("destination", BASE + destination_offset)
    if case["name"] == "overlap":
        destination = source
    golden = transform(pixels, width, height, case["mode"], case["threshold"]) if valid else b""
    expected = bytearray(original)
    events, reads, writes, read_bytes, written_bytes, read_beats, write_beats = [], 0, 0, 0, 0, 0, 0
    longest_read = longest_write = 0
    loaded_rows = 0
    stopped = False
    for row in range(height if valid else 0):
        needed = min(height - 1, row + (case["mode"] == 3))
        while loaded_rows <= needed and not stopped:
            for address, beats, _ in row_bursts(source + loaded_rows * source_stride, (width + 7) & ~7, maximum):
                reads += 1; read_bytes += beats * 8; read_beats += beats
                longest_read = max(longest_read, beats)
                events.append(("AR", address, beats))
                if reads == case["fail_read"] or case["scenario"] == 4:
                    stopped = True
                    break
            loaded_rows += 1
        if stopped:
            break
        for address, beats, enabled in row_bursts(destination + row * destination_stride, width, maximum):
            writes += 1; write_beats += beats
            longest_write = max(longest_write, beats)
            events.append(("AW", address, beats))
            if writes == case["fail_write"]:
                stopped = True
                break
            column = address - destination - row * destination_stride
            expected[address - BASE:address - BASE + enabled] = golden[row * width + column:row * width + column + enabled]
            written_bytes += enabled
        if stopped:
            break
    repetitions = 100 if case["scenario"] == 2 else 1
    counters = dict(reads=reads * repetitions, writes=writes * repetitions, read_bytes=read_bytes,
                    written_bytes=written_bytes, read_beats=read_beats * repetitions, write_beats=write_beats * repetitions,
                    max_read=longest_read, max_write=longest_write, error=case["error"], repetitions=repetitions)
    configuration = [width, height, source_stride, destination_stride, case["mode"], case["threshold"], source,
                     destination, case["scenario"], case["error"],
                     0 if case["name"] == "read-unmapped" else case["fail_read"],
                     0 if case["name"] == "write-unmapped" else case["fail_write"]]
    if case["scenario"] == 2:
        orders = [prepare(dict(case, scenario=0, mode=mode), seed, maximum)[4] for mode in range(4)]
        events = [event for repeat in range(repetitions) for event in orders[repeat % 4]]
    return original, expected, configuration, counters, events, pixels, destination_offset


def lint():
    for maximum in (1, 4, 16):
        lab.logged(["verilator", "--lint-only", "--assert", "--Wall", "--top-module", "framefx_core",
                    f"-GMAX_BEATS={maximum}", *SOURCES], lab.ARTIFACTS / f"logs/framefx-lint-{maximum}.log", timeout=30)


def build(jobs, maximum=16):
    directory = lab.ARTIFACTS / f"build/framefx-{maximum}"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "framefx-test"
    lab.logged(["verilator", "--cc", "--exe", "--build", "--assert", "--trace", "--timescale", "1ns/1ps",
                "--Wall", "--Mdir", directory, "--top-module", "framefx_core", f"-GMAX_BEATS={maximum}",
                "-o", binary.name, "-CFLAGS", "-std=c++17", "-j", str(jobs), *SOURCES, lab.ROOT / "sim/framefx_test.cpp"],
               lab.ARTIFACTS / f"logs/framefx-build-{maximum}.log", timeout=180)
    return binary


def pixel_test(jobs):
    directory = lab.ARTIFACTS / "build/pixel"
    directory.mkdir(parents=True, exist_ok=True)
    lab.logged(["verilator", "--cc", "--exe", "--build", "--Wall", "--Mdir", directory, "--top-module", "framefx_pixel",
                "-o", "pixel-test", "-CFLAGS", "-std=c++17", "-j", str(jobs), SOURCES[0], lab.ROOT / "sim/pixel_test.cpp"],
               lab.ARTIFACTS / "logs/pixel-build.log", timeout=120)
    lab.logged([directory / "pixel-test"], lab.ARTIFACTS / "logs/pixel-test.log", timeout=15)
    if "PIXEL_PASS cases=76304" not in (lab.ARTIFACTS / "logs/pixel-test.log").read_text():
        raise RuntimeError("Missing pixel arithmetic completion marker")


def synthesize():
    vivado = lab.vivado_path()
    if not vivado:
        raise RuntimeError("Vivado not found; set VIVADO=/absolute/path/to/bin/vivado")
    directory = lab.ARTIFACTS / "build/framefx-synth"
    directory.mkdir(parents=True, exist_ok=True)
    result = dict(schema=1, test="framefx-out-of-context-synthesis", status="FAIL", time_utc=lab.timestamp(),
                  part="xczu3eg-sbva484-1-e", hardware_tested=False, timing_closed=False,
                  vivado=lab.capture([vivado, "-version"], 30),
                  source_sha256={str(source.relative_to(lab.ROOT)): lab.sha256(source)
                                 for source in [*SOURCES, lab.ROOT / "fpga/ultra96-v1/framefx-synth.tcl"]})
    try:
        lab.logged([vivado, "-mode", "batch", "-nojournal", "-nolog", "-source",
                    lab.ROOT / "fpga/ultra96-v1/framefx-synth.tcl", "-tclargs", lab.ROOT / "rtl/core"],
                   lab.ARTIFACTS / "logs/framefx-synth.log", cwd=directory, timeout=300)
        log = (lab.ARTIFACTS / "logs/framefx-synth.log").read_text()
        if "FRAMEFX_SYNTHESIS_PASS" not in log or not (directory / "framefx-synth.dcp").is_file():
            raise RuntimeError("Missing synthesis outputs")
        result["warnings"] = [line for line in log.splitlines() if line.startswith(("WARNING:", "CRITICAL WARNING:"))]
        result["status"] = "PASS"
    finally:
        lab.write_json(lab.ARTIFACTS / "framefx-synth.json", result)


def run_case(directory, binary, case, seed, maximum, tracing=False):
    directory = directory.resolve()
    directory.mkdir(parents=True)
    original, expected, configuration, counters, events, pixels, destination_offset = prepare(case, seed, maximum)
    (directory / "input.bin").write_bytes(original)
    (directory / "expected.bin").write_bytes(expected)
    if case["scenario"] == 2:
        for mode in range(4):
            (directory / f"expected-mode-{mode}.bin").write_bytes(prepare(dict(case, scenario=0, mode=mode), seed, maximum)[1])
    recovery = case["error"] in (2, 3) and "unmapped" not in case["name"]
    if recovery:
        successful = dict(case, error=0, fail_read=0, fail_write=0)
        (directory / "recovery.bin").write_bytes(prepare(successful, seed, maximum)[1])
    (directory / "config.txt").write_text(" ".join(map(str, configuration)) + "\n")
    lab.write_json(directory / "case.json", dict(case, seed=seed, maximum=maximum))
    command = [binary, directory, str(seed ^ case["id"])] + (["trace"] if tracing else [])
    lab.write_json(directory / "command.json", list(map(str, command)))
    try:
        lab.logged(command, directory / "console.log", timeout=60)
        if "FRAMEFX_PASS" not in (directory / "console.log").read_text():
            raise RuntimeError("Missing completion marker")
        if recovery and "FRAMEFX_RECOVERY_PASS" not in (directory / "console.log").read_text():
            raise RuntimeError("Missing DMA error recovery marker")
        actual = (directory / "actual.bin").read_bytes()
        if actual != expected:
            mismatch = next((offset for offset, (wanted, observed) in enumerate(zip(expected, actual)) if wanted != observed),
                            min(len(expected), len(actual)))
            raise RuntimeError(f"Independent full-RAM scoreboard failed at byte {mismatch}")
        result = json.loads((directory / "rtl.json").read_text())
        if result.get("status") != "PASS" or any(result.get(key) != value for key, value in counters.items()):
            raise RuntimeError(f"Counter mismatch: expected={counters} actual={result}")
        observed = [(kind, int(address), int(beats)) for kind, address, beats in
                    re.findall(r"^\d+ (AR|AW) address=(\d+) beats=(\d+)", (directory / "events.log").read_text(), re.MULTILINE)]
        if observed != events:
            raise RuntimeError("Independent row/burst address scoreboard failed")
        if case["error"] == 0:
            output = b"".join(actual[destination_offset + row * case["destination_stride"]:
                                     destination_offset + row * case["destination_stride"] + case["width"]] for row in range(case["height"]))
            header = f"P5\n{case['width']} {case['height']}\n255\n".encode()
            (directory / "input.pgm").write_bytes(header + pixels)
            (directory / "output.pgm").write_bytes(header + output)
            result["input_sha256"] = lab.sha256(directory / "input.pgm")
            result["output_sha256"] = lab.sha256(directory / "output.pgm")
        if tracing and (directory / "wave.vcd").stat().st_size < 1000:
            raise RuntimeError("Missing waveform")
        lab.write_json(directory / "result.json", result)
        return result
    except Exception as error:
        lab.write_json(directory / "result.json", dict(status="FAIL", error=str(error), seed=seed, case=case["id"]))
        if not tracing:
            replay = directory / "failure-trace"
            replay.mkdir()
            for name in ("input.bin", "expected.bin", "config.txt"):
                shutil.copyfile(directory / name, replay / name)
            if recovery:
                shutil.copyfile(directory / "recovery.bin", replay / "recovery.bin")
            if case["scenario"] == 2:
                for mode in range(4):
                    shutil.copyfile(directory / f"expected-mode-{mode}.bin", replay / f"expected-mode-{mode}.bin")
            try:
                lab.logged([binary, replay, str(seed ^ case["id"]), "trace"], replay / "console.log", timeout=60)
            except Exception:
                pass
        raise


def test(jobs):
    lint()
    pixel_test(jobs)
    binaries = {maximum: build(jobs, maximum) for maximum in (1, 4, 16)}
    evidence = lab.ARTIFACTS / datetime.datetime.now(datetime.timezone.utc).strftime("framefx-%Y%m%dT%H%M%S-%fZ")
    evidence.mkdir()
    result = dict(schema=1, test="M3-framefx-standalone", status="FAIL", time_utc=lab.timestamp(),
                  hardware_tested=False, linux_framefx_tested=False, cases=0, seeds=[1, 42, 2026])
    started = time.monotonic()
    try:
        lab.write_json(evidence / "versions.json", dict(lock=lab.LOCK, project_sha256=lab.project_hashes(),
                       binary_sha256={str(binary.relative_to(lab.ROOT)): lab.sha256(binary)
                                      for binary in [*binaries.values(), lab.ARTIFACTS / "build/pixel/pixel-test"]}))
        plans = cases()
        for seed in result["seeds"]:
            for case in plans:
                run_case(evidence / f"seed-{seed}/case-{case['id']}", binaries[16], case, seed, 16)
                result["cases"] += 1
        selected = [case for case in plans if case["name"] != "image" or (case["width"] in (7, 65, 1024) and case["height"] == 3)]
        for maximum in (1, 4):
            for case in selected:
                run_case(evidence / f"max-{maximum}/case-{case['id']}", binaries[maximum], case, 42, maximum)
                result["cases"] += 1
        trace_case = next(case for case in plans if case["mode"] == 3 and case["width"] == 65 and case["height"] == 9)
        original = evidence / f"seed-1/case-{trace_case['id']}"
        replay = run_case(evidence / "trace", binaries[16], trace_case, 1, 16, True)
        if replay != json.loads((original / "result.json").read_text()) or (evidence / "trace/events.log").read_bytes() != (original / "events.log").read_bytes():
            raise RuntimeError("Traced replay changed cycle counts/results/bus history")
        result.update(status="PASS", deterministic_trace_replay=True, parameter_limits=[1, 4, 16])
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        lab.write_json(evidence / "result.json", result)
        lab.write_json(lab.ARTIFACTS / "latest-framefx.json", dict(directory=str(evidence.relative_to(lab.ROOT)), **result))
        print(f"{result['status']}: {evidence.relative_to(lab.ROOT)}")
