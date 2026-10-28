#!/usr/bin/env python3
import argparse
import datetime
import json
from pathlib import Path
import subprocess


def read_text(root, name, limit=65536):
    try:
        with (root / name).open("rb") as stream:
            return stream.read(limit).decode("utf-8", errors="replace").strip("\x00\n ")
    except OSError:
        return ""


def collect(root=Path("/")):
    model = read_text(root, "proc/device-tree/model")
    compatible = read_text(root, "proc/device-tree/compatible").split("\x00")
    connectors = []
    for filename in sorted((root / "sys/class/drm").glob("card*-*/status")):
        connectors.append(dict(name=filename.parent.name, status=read_text(root, str(filename.relative_to(root))),
                               modes=read_text(root, str((filename.parent / "modes").relative_to(root))).splitlines()))
    memory = {}
    for line in read_text(root, "proc/meminfo").splitlines():
        key, _, value = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "CmaTotal", "CmaFree"):
            memory[key] = value.strip()
    return dict(schema=1, stage="H0", time_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                model=model, compatible=compatible, kernel=read_text(root, "proc/sys/kernel/osrelease"),
                board_identity="MATCH" if "avnet,ultra96-rev1" in compatible else "UNVERIFIED",
                cmdline=read_text(root, "proc/cmdline"), uptime=read_text(root, "proc/uptime"), memory=memory,
                connectors=connectors, display_visible="REQUIRES_OPERATOR_OBSERVATION",
                framefx="NOT_LOADED_NOT_TESTED", physical_dma="NOT_TESTED", physical_irq="NOT_TESTED")


def main():
    parser = argparse.ArgumentParser(description="Read-only Ultra96 H0 boot diagnostics; no PL/MMIO access")
    parser.add_argument("--output", type=Path, default=Path("/var/lib/axi-lab"))
    arguments = parser.parse_args()
    report = collect()
    for name, command in (("dmesg", ["dmesg"]), ("ip-addresses", ["ip", "-j", "address", "show"]),
                          ("lsblk", ["lsblk", "-J"]), ("uname", ["uname", "-a"])):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
            report[name] = dict(returncode=result.returncode, output=result.stdout[-131072:], error=result.stderr[-4096:])
        except (OSError, subprocess.TimeoutExpired) as error:
            report[name] = dict(error=str(error))
    arguments.output.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output / "boot-report.json.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(arguments.output / "boot-report.json")
    public = {name: report[name] for name in ("stage", "model", "kernel", "board_identity", "connectors", "framefx", "memory")}
    script = Path("/home/xilinx/axi-lab-status.js")
    script.write_text("window.AXI_LAB_STATUS = " + json.dumps(public).replace("<", "\\u003c") + ";\n")
    script.chmod(0o644)
    print("AXI_LAB_H0_DIAGNOSTICS identity=" + report["board_identity"] + " display=OPERATOR_CHECK framefx=NOT_TESTED", flush=True)


if __name__ == "__main__":
    main()
