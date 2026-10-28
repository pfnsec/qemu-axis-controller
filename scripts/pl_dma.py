import argparse
import json
import math
from pathlib import Path
import re

import demo
import lab


PROFILES = {
    "copy-16": dict(width=16, height=16, seed=9601, mode=0, threshold=127, repeat=1),
    "minimum": dict(width=3, height=3, seed=9610, mode=4, threshold=127, repeat=1),
    "tail": dict(width=7, height=5, seed=9620, mode=4, threshold=127, repeat=2),
    "narrow": dict(width=65, height=9, seed=9700, mode=4, threshold=127, repeat=25),
    "page-crossing": dict(width=513, height=17, seed=9800, mode=4, threshold=127, repeat=2),
    "video": dict(width=320, height=240, seed=9900, mode=4, threshold=127, repeat=4),
    "maximum": dict(width=1024, height=1024, seed=10000, mode=4, threshold=127, repeat=1),
    "threshold-zero": dict(width=65, height=9, seed=10100, mode=4, threshold=0, repeat=1),
    "threshold-255": dict(width=65, height=9, seed=10200, mode=4, threshold=255, repeat=1),
}
FATAL = r"rcu.*(?:stall|starved)|\bSError\b|\bKernel panic\b|\bOops:|\bBUG:|WARNING: CPU:|FRAMEFX_QUARANTINE|DMA-API:"


def irq_count(interrupts):
    entries = re.findall(r"^\s*\d+:\s+((?:\d+\s+)+)GICv[23]\s+121\s+Level\s+([^\n]+)$",
                         interrupts, re.MULTILINE)
    if len(entries) != 1 or entries[0][1].strip() != "framefx":
        raise RuntimeError("Expected exclusive framefx GIC 121 ownership")
    return sum(int(value) for value in entries[0][0].split())


def validate_health(before, after, count):
    boot_id = before["/proc/sys/kernel/random/boot_id"].strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id) or \
            after["/proc/sys/kernel/random/boot_id"].strip() != boot_id:
        raise RuntimeError("Boot identity changed or unavailable")
    for snapshot in (before, after):
        if snapshot["/sys/module/framefx/refcnt"].strip() != "0":
            raise RuntimeError("Framefx ownership or quarantine reference remains")
        if re.search(FATAL, snapshot["dmesg"], re.IGNORECASE):
            raise RuntimeError("Kernel fault or DMA diagnostic detected")
    start, end = (irq_count(snapshot["/proc/interrupts"]) for snapshot in (before, after))
    if end - start != count:
        raise RuntimeError("Delivered interrupt count does not match completed jobs")
    return dict(boot_id=boot_id, irq_before=start, irq_after=end, irq_delta=count,
                ownership_released=True, exact_cma_balance="NOT_MEASURED")


def validate_profile(directory, profile_name):
    profile = PROFILES[profile_name]
    count = profile["repeat"] * (4 if profile["mode"] == 4 else 1)
    expected_files = {f"{kind}-{index}.{extension}" for index in range(count)
                      for kind, extension in (("job", "json"), ("input", "pgm"), ("output", "pgm"),
                                              ("source", "bin"), ("destination", "bin"))}
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("Unsafe export directory")
    files = list(directory.iterdir())
    if {path.name for path in files} != expected_files or any(path.is_symlink() or not path.is_file() for path in files):
        raise RuntimeError("Incorrect physical export manifest")
    records = []
    for index in range(count):
        record = demo.validate_image(directory / f"job-{index}.json")
        expected = {key: profile[key] for key in ("width", "height", "threshold")}
        expected.update(seed=profile["seed"] + index, mode=index % 4 if profile["mode"] == 4 else profile["mode"])
        if any(record[key] != value for key, value in expected.items()):
            raise RuntimeError("Incorrect physical image coverage")
        for kind, padding in (("source", 64), ("destination", 128)):
            stride = ((profile["width"] + 63) & ~63) + padding
            size = (stride * profile["height"] + 4096 + 4095) & ~4095
            if record[f"{kind}_stride"] != stride or record[f"{kind}_bytes"] != size:
                raise RuntimeError("Unexpected native allocation geometry")
        elapsed = record.get("elapsed_ms")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 < elapsed <= 1000:
            raise RuntimeError("Missing or invalid elapsed time")
        if records and any(record[key] != records[-1][key] + 1 for key in ("irq_count", "job_id")):
            raise RuntimeError("Nonsequential completion counters")
        records.append(record)
    return dict(profile=profile_name, status="PASS", jobs=count, images=records,
                artifact_sha256={path.name: lab.sha256(path) for path in sorted(files)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline physical DMA image verification; never connects to or programs hardware")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--profile", required=True, choices=PROFILES)
    arguments = parser.parse_args()
    print(json.dumps(validate_profile(arguments.directory, arguments.profile), indent=2))
