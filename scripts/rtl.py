import datetime
import json
import re
import time

import lab


SOURCES = [lab.ROOT / "rtl/core/axi_burst_copy.sv", lab.ROOT / "rtl/spike/axi_spike.sv"]
BINARY = lab.ARTIFACTS / "build/rtl-test/rtl-test"


class CaseFailure(RuntimeError):
    def __init__(self, case_id, message):
        self.case_id = case_id
        super().__init__(f"RTL case {case_id}: {message}")


def lint():
    lab.logged(["verilator", "--lint-only", "--assert", "--Wall", "--top-module", "axi_spike", *SOURCES],
               lab.ARTIFACTS / "logs/rtl-lint.log", timeout=30)
    for maximum in (1, 4, 16):
        lab.logged(["verilator", "--lint-only", "--assert", "--Wall", "--top-module", "axi_burst_copy",
                    f"-GMAX_BEATS={maximum}", SOURCES[0]], lab.ARTIFACTS / f"logs/rtl-lint-{maximum}.log", timeout=30)


def build(jobs):
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    lab.logged(["verilator", "--cc", "--exe", "--build", "--assert", "--trace", "--timescale", "1ns/1ps",
                "--Wall", "--Mdir", BINARY.parent, "--top-module", "axi_spike", "-o", BINARY.name,
                "-CFLAGS", "-std=c++17", "-j", str(jobs), *SOURCES, lab.ROOT / "sim/rtl_test.cpp"],
               lab.ARTIFACTS / "logs/rtl-build.log", timeout=180)


def expected_cases():
    tests = []
    for beats in (1, 2, 15, 16, 17, 31, 32, 33, 257):
        for source in (64, 0xf80, 0xff8):
            for destination in (0x4040, 0x4fc0, 0x4ff8):
                for mask in (0, 0x55, 0xaa, 0xff):
                    tests.append(dict(name="burst", source=source, destination=destination, beats=beats,
                                      mask=mask, error=0, written=beats))
    for name, source, destination, beats, error, written in (
        ("read-error-first", 64, 0x4040, 33, 2, 0), ("write-error-first", 64, 0x4040, 33, 3, 0),
        ("read-error-second", 64, 0x4040, 33, 2, 16), ("write-error-second", 64, 0x4040, 33, 3, 16),
        ("read-unmapped", 65536, 0x4040, 16, 2, 0), ("write-unmapped", 64, 65536, 16, 3, 0),
        ("zero-length", 64, 0x4040, 0, 1, 0), ("misaligned", 65, 0x4040, 16, 1, 0),
        ("overlap", 64, 64, 16, 1, 0), ("overflow", 64, 0x4040, 2, 1, 0),
        ("bad-rlast", 64, 0x4040, 1, 4, 0)):
        tests.append(dict(name=name, source=source, destination=destination, beats=beats,
                          mask=255, error=error, written=written))
    for phase in ("ar", "r", "aw", "w", "b", "lite-aw", "lite-w", "lite-b", "lite-r", "irq"):
        tests.append(dict(name=f"reset-{phase}", source=64, destination=0x4040, beats=16,
                          mask=255, error=0, written=16))
    tests.append(dict(name="consecutive-100", source=64, destination=0x4040, beats=1, mask=255, error=0, written=1))
    return tests


def bursts(source, destination, count):
    transfers = []
    while count:
        size = min(128, count, 4096 - source % 4096, 4096 - destination % 4096)
        transfers.append((source, destination, size // 8))
        source += size
        destination += size
        count -= size
    return transfers


def validate(directory, seed, selected=None):
    plans = expected_cases()
    observed = [json.loads(line) for line in (directory / "cases.jsonl").read_text().splitlines()]
    expected_ids = list(range(len(plans))) if selected is None else [selected]
    if [case.get("id") for case in observed] != expected_ids:
        raise RuntimeError("Missing, duplicate or reordered RTL cases")
    initial = {}
    events = (directory / "events.log").read_text()
    addresses = {}
    for match in re.finditer(r"^(\d+) \d+ (AR|AW) address=(\d+) beats=(\d+)", events, re.MULTILINE):
        case, channel, address, count = match.groups()
        addresses.setdefault((int(case), channel), []).append((int(address), int(count)))
    for actual in observed:
        def reject(message):
            raise CaseFailure(actual["id"], message)

        plan = plans[actual["id"]]
        if any(actual.get(key) != value for key, value in plan.items()) or actual.get("seed") != seed or actual.get("status") != "PASS":
            reject(f"RTL case metadata mismatch: {actual}")
        fill_seed = (seed + 99) & 0xffffffff if plan["name"] == "consecutive-100" else seed
        if actual.get("fill_seed") != fill_seed:
            reject("Unexpected RAM initialization seed")
        if fill_seed not in initial:
            initial[fill_seed] = bytes(((offset * 37 + fill_seed * 17) ^ (offset >> 8)) & 255 for offset in range(65536))
        expected = bytearray(initial[fill_seed])
        for offset in range(plan["written"] * 8):
            if plan["mask"] & (1 << (offset % 8)):
                expected[plan["destination"] + offset] = initial[fill_seed][plan["source"] + offset] ^ ((0xd3c2b1a08796e5f4 >> (8 * (offset % 8))) & 255)
        snapshot_name = f"case-{actual['id']}.bin"
        if actual.get("snapshot") != snapshot_name:
            reject("Unexpected snapshot path")
        try:
            snapshot = (directory / snapshot_name).read_bytes()
        except OSError as error:
            reject(f"Cannot read snapshot: {error}")
        if snapshot != expected:
            mismatch = next((index for index, pair in enumerate(zip(snapshot, expected)) if pair[0] != pair[1]), min(len(snapshot), len(expected)))
            reject(f"Independent RAM scoreboard failed: byte={mismatch}")
        if plan["name"] == "burst":
            expected_bursts = bursts(0x100000000 + plan["source"], 0x100000000 + plan["destination"], plan["beats"] * 8)
            for channel, position in (("AR", 0), ("AW", 1)):
                wanted = [(transfer[position], transfer[2]) for transfer in expected_bursts]
                if addresses.get((actual["id"], channel)) != wanted:
                    reject(f"Burst/address scoreboard failed: channel={channel}")
            if actual["reads"] != len(expected_bursts) or actual["writes"] != len(expected_bursts):
                reject("Burst was not coalesced to exactly one backend call")
            if actual["read_beats"] != plan["beats"] or actual["write_beats"] != plan["beats"]:
                reject("Incorrect beat accounting")
        if plan["name"] == "read-error-first" and (actual["read_beats"], actual["writes"]) != (16, 0):
            reject("Failed read burst was not drained without writing")
        if plan["name"] == "read-error-second" and (actual["read_beats"], actual["write_beats"]) != (32, 16):
            reject("Second read failure did not stop at the failed burst")
        if not 0 <= actual["max_read"] <= 16 or not 0 <= actual["max_write"] <= 16:
            reject("Unsupported burst reached backend")
    return observed


def run(directory, seed, selected=None):
    directory.mkdir(parents=True)
    command = [BINARY, str(seed), directory]
    if selected is not None:
        command.append(str(selected))
    lab.write_json(directory / "command.json", [str(value) for value in command])
    try:
        lab.logged(command, directory / "console.log", timeout=60)
        expected_count = len(expected_cases()) if selected is None else 1
        if f"RTL_SUITE_PASS seed={seed} cases={expected_count}" not in (directory / "console.log").read_text():
            raise RuntimeError("Missing RTL completion marker")
        return validate(directory, seed, selected)
    except Exception as error:
        if selected is None:
            console_file = directory / "console.log"
            console = console_file.read_text(errors="replace") if console_file.exists() else ""
            started = re.findall(r"RTL_CASE_BEGIN id=(\d+)", console)
            failed_case = error.case_id if isinstance(error, CaseFailure) else int(started[-1]) if started else None
            if failed_case is not None:
                try:
                    run(directory / "failure-trace", seed, failed_case)
                except Exception:
                    pass
        raise


def test(jobs):
    lint()
    build(jobs)
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("rtl-%Y%m%dT%H%M%S-%fZ")
    evidence = lab.ARTIFACTS / run_id
    evidence.mkdir()
    result = {"schema": 1, "test": "M2-standalone-AXI-fabric", "status": "FAIL", "time_utc": lab.timestamp(),
              "framefx_tested": False, "hardware_tested": False, "seeds": []}
    started = time.monotonic()
    try:
        lab.write_json(evidence / "versions.json", {"lock": lab.LOCK, "project_sha256": lab.project_hashes(),
                       "binary_sha256": {str(BINARY.relative_to(lab.ROOT)): lab.sha256(BINARY)}})
        for seed in (1, 42, 2026, 0xdeadbeef):
            cases = run(evidence / f"seed-{seed}", seed)
            result["seeds"].append({"seed": seed, "cases": len(cases), "cycles": sum(case["cycles"] for case in cases), "status": "PASS"})
        repeat = run(evidence / "repeat-1", 1)
        original = validate(evidence / "seed-1", 1)
        if repeat != original or (evidence / "repeat-1/events.log").read_bytes() != (evidence / "seed-1/events.log").read_bytes():
            raise RuntimeError("Seed replay changed cycle counts or bus transitions")
        trace_case = 323
        traced = run(evidence / "trace", 1, trace_case)
        if traced != [original[trace_case]]:
            raise RuntimeError("Tracing changed model behavior")
        if not (evidence / "trace/wave.vcd").is_file() or (evidence / "trace/wave.vcd").stat().st_size < 1000:
            raise RuntimeError("Missing waveform")
        result.update({"status": "PASS", "deterministic_replay": True, "trace_checked": True,
                       "unique_seeded_cases": sum(item["cases"] for item in result["seeds"])})
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        lab.write_json(evidence / "result.json", result)
        lab.write_json(lab.ARTIFACTS / "latest-rtl.json", {"directory": str(evidence.relative_to(lab.ROOT)), **result})
        print(f"{result['status']}: {evidence.relative_to(lab.ROOT)}")
