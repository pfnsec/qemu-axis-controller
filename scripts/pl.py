import array
import datetime
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import zipfile
import xml.etree.ElementTree as ET

import lab
import framefx


DIRECTORY = lab.ROOT / "fpga/ultra96-v1"
PLATFORM = json.loads((DIRECTORY / "platform.json").read_text())


def sources():
    paths = [DIRECTORY / name for name in ("create_project.tcl", "create_bd.tcl", "platform.json",
                                          "framefx.dtso", "framefx-afi.dtso")]
    paths += list((lab.ROOT / "rtl/core").glob("framefx_*.sv"))
    paths += [lab.ROOT / "rtl/board/framefx_ultra96.sv", Path(__file__).resolve()]
    paths += [lab.SOURCES / "avnet-bdf-ultra96v1/ultra96v1/1.2" / name
              for name in ("board.xml", "preset.xml", "part0_pins.xml")]
    return {str(path.relative_to(lab.ROOT)): lab.sha256(path) for path in sorted(paths)}


def bit_header(data):
    offset = 0

    def take(count):
        nonlocal offset
        if count < 0 or offset + count > len(data):
            raise ValueError("Truncated bitstream header")
        value = data[offset:offset + count]
        offset += count
        return value

    if take(struct.unpack(">H", take(2))[0]) != bytes.fromhex("0ff00ff00ff00ff000"):
        raise ValueError("Invalid bitstream preamble")
    if take(2) != b"\x00\x01":
        raise ValueError("Invalid bitstream header marker")
    result = {}
    for field, name in ((b"a", "design"), (b"b", "part"), (b"c", "date"), (b"d", "time")):
        if take(1) != field:
            raise ValueError("Unexpected bitstream header field")
        value = take(struct.unpack(">H", take(2))[0])
        if not value.endswith(b"\0") or b"\0" in value[:-1]:
            raise ValueError("Invalid bitstream string")
        result[name] = value[:-1].decode("ascii")
    if take(1) != b"e":
        raise ValueError("Missing bitstream payload")
    length = struct.unpack(">I", take(4))[0]
    payload = take(length)
    if offset != len(data) or length < 256 or length % 4 or b"\xaa\x99\x55\x66" not in payload[:256]:
        raise ValueError("Invalid bitstream payload length/synchronization")
    if result["part"] not in (PLATFORM["part"], PLATFORM["part"].removeprefix("xc")):
        raise ValueError("Bitstream is not for the reviewed Ultra96 V1 part")
    return {**result, "payload_bytes": length}


def check_hwh(path):
    root = ET.parse(path)
    system = root.find("./SYSTEMINFO")
    if system is None or system.get("DEVICE") != "xczu3eg" or system.get("PACKAGE") != "sbva484" or \
            system.get("BOARD") != PLATFORM["board_part"] or len(root.findall("./EXTERNALPORTS/PORT")):
        raise ValueError("Wrong board or unexpected external PL IO")
    modules = {module.get("INSTANCE"): module for module in root.findall(".//MODULE")}

    def parameters_of(name):
        return {value.get("NAME"): value.get("VALUE")
                for value in modules[name].findall("./PARAMETERS/PARAMETER")}

    def net(endpoint, kind="PORTS/PORT", attribute="SIGNAME"):
        module, name = endpoint.split("/")
        values = [item.get(attribute) for item in modules[module].findall("./" + kind) if item.get("NAME") == name]
        if len(values) != 1 or not values[0]:
            raise ValueError(f"Unconnected handoff endpoint: {endpoint}")
        return values[0]

    def connected(endpoints, kind="PORTS/PORT", attribute="SIGNAME"):
        if len({net(endpoint, kind, attribute) for endpoint in endpoints}) != 1:
            raise ValueError(f"Handoff connection mismatch: {endpoints}")

    processor = modules["ps"]
    if processor.get("MODTYPE") != "zynq_ultra_ps_e":
        raise ValueError("Missing ZynqMP PS")
    parameters = parameters_of("ps")
    expected = {"PSU__DDRC__MEMORY_TYPE": "LPDDR 4", "PSU__DDRC__BUS_WIDTH": "32 Bit",
                "PSU__DDRC__DEVICE_CAPACITY": "8192 MBits", "PSU__DDRC__T_RC": "64",
                "PSU__USE__M_AXI_GP0": "0", "PSU__USE__M_AXI_GP1": "0", "PSU__USE__M_AXI_GP2": "1",
                "PSU__MAXIGP2__DATA_WIDTH": "32", "PSU__USE__S_AXI_GP2": "1",
                "PSU__SAXIGP2__DATA_WIDTH": "128", "PSU__USE__IRQ0": "1",
                "PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ": "100", "PSU__FPGA_PL0_ENABLE": "1",
                "PSU__SD1__PERIPHERAL__IO": "MIO 46 .. 51", "PSU__USB0__PERIPHERAL__IO": "MIO 52 .. 63",
                "PSU__USB1__PERIPHERAL__IO": "MIO 64 .. 75", "PSU__DPAUX__PERIPHERAL__IO": "MIO 27 .. 30"}
    for name, value in expected.items():
        if parameters.get(name) != value:
            raise ValueError(f"Unexpected PS setting {name}: {parameters.get(name)}")
    ranges = processor.findall("./MEMORYMAP/MEMRANGE")
    control = [item for item in ranges if item.get("INSTANCE") == "accelerator"]
    if len(control) != 1 or int(control[0].get("BASEVALUE"), 16) != PLATFORM["control"]["base"] or \
            int(control[0].get("HIGHVALUE"), 16) != PLATFORM["control"]["base"] + 4095 or \
            control[0].get("MASTERBUSINTERFACE") != PLATFORM["control"]["port"]:
        raise ValueError("Unexpected Vivado control address assignment")
    memory = modules["accelerator"].findall("./MEMORYMAP/MEMRANGE")
    if len(memory) != 1 or memory[0].get("SLAVEBUSINTERFACE") != "S_AXI_HP0_FPD" or \
            int(memory[0].get("BASEVALUE"), 16) != 0 or int(memory[0].get("HIGHVALUE"), 16) != 0x7fffffff:
        raise ValueError("DMA map must contain only the low 2 GiB DDR segment")
    reset = parameters_of("reset")
    if reset.get("C_EXT_RESET_HIGH") != "0" or reset.get("C_AUX_RESET_HIGH") != "0":
        raise ValueError("Unexpected reset polarity")
    if int(parameters_of("one").get("CONST_VAL", "-1"), 0) != 1 or int(parameters_of("zero").get("CONST_VAL", "-1"), 0) != 0:
        raise ValueError("Unexpected reset/interrupt constants")
    connected(["ps/pl_clk0", "ps/maxihpm0_lpd_aclk", "ps/saxihp0_fpd_aclk", "accelerator/clk",
               "control/aclk", "memory/aclk", "reset/slowest_sync_clk"])
    connected(["ps/pl_resetn0", "reset/ext_reset_in"])
    connected(["one/dout", "reset/aux_reset_in", "reset/dcm_locked"])
    connected(["zero/dout", "reset/mb_debug_sys_rst", *[f"interrupts/In{index}" for index in range(1, 8)]])
    connected(["reset/peripheral_aresetn", "accelerator/reset_n", "control/aresetn", "memory/aresetn"])
    connected(["accelerator/irq", "interrupts/In0"])
    connected(["interrupts/dout", "ps/pl_ps_irq0"])
    for endpoints in (("ps/M_AXI_HPM0_LPD", "control/S00_AXI"), ("control/M00_AXI", "accelerator/S_AXI"),
                      ("accelerator/M_AXI", "memory/S00_AXI"), ("memory/M00_AXI", "ps/S_AXI_HP0_FPD")):
        connected(endpoints, "BUSINTERFACES/BUSINTERFACE", "BUSNAME")
    return {"processor": processor.get("VLNV"), "checked_parameters": expected,
            "control": control[0].attrib, "memory": memory[0].attrib,
            "clock_reset_irq_connections": "PASS"}


def check_device_tree(path):
    tool = lab.ARTIFACTS / "build/guest/host/bin/fdtget"

    def query(node, property_name=None, mode="x", option="-t"):
        command = [tool, option]
        if option == "-t":
            command.append(mode)
        command.extend([path, node])
        if property_name is not None:
            command.append(property_name)
        return subprocess.check_output(command, text=True, timeout=10).split()

    region = "/fpga-full"
    device = region + "/framefx@80000000"
    if query(region, option="-l") != ["framefx@80000000"]:
        raise ValueError("Framefx must be a direct child of the populated FPGA region")
    clock = query("/__symbols__", "zynqmp_clk", mode="s")[0]
    interrupt = query("/__symbols__", "gic", mode="s")[0]
    expected = {(region, "#address-cells"): ["2"], (region, "#size-cells"): ["2"],
                (region, "dma-ranges"): ["0", "0", "0", "0", "0", "80000000"],
                (device, "reg"): ["0", "80000000", "0", "1000"],
                (device, "interrupts"): ["0", "59", "4"],
                (device, "interrupt-parent"): query(interrupt, "phandle"),
                (device, "clocks"): query(clock, "phandle") + ["47"],
                (device, "assigned-clocks"): query(clock, "phandle") + ["47"],
                (device, "assigned-clock-rates"): ["5f5e100"],
                ("/axi-lab-afi", "config-afi"): ["4", "0", "5", "0", "f", "0"]}
    for (node, property_name), value in expected.items():
        if query(node, property_name) != value:
            raise ValueError("Incorrect device-tree property: " + node + "/" + property_name)
    for node, property_name, value in ((region, "compatible", "fpga-region"),
                                      (device, "compatible", "example,axi-framefx-1.0"),
                                      (device, "status", "okay"),
                                      ("/axi-lab-afi", "compatible", "xlnx,afi-fpga")):
        if query(node, property_name, mode="s") != [value]:
            raise ValueError("Unexpected device-tree identity: " + node)
    if any("dma-coherent" in query(node, option="-p") for node in ("/", region, device)):
        raise ValueError("HP0 must not be marked DMA coherent")
    return dict(device=device, direct_region_child=True, dma_limit=0x7fffffff, coherent=False)


def package(directory, initial_sources):
    if initial_sources != sources():
        raise RuntimeError("PL sources changed during build")
    console = (directory / "vivado.log").read_text(errors="replace")
    if "FRAMEFX_BITSTREAM_BUILT xczu3eg-sbva484-1-e HARDWARE_NOT_TESTED" not in console:
        raise RuntimeError("Vivado did not reach routed timing/DRC/bitstream acceptance")
    if re.search(r"^(ERROR|CRITICAL WARNING):", console, re.MULTILINE):
        raise RuntimeError("Unreviewed Vivado error/critical warning")
    if "All user specified timing constraints are met." not in (directory / "timing.rpt").read_text():
        raise RuntimeError("Routed timing summary did not pass")
    header = bit_header((directory / "framefx.bit").read_bytes())
    with zipfile.ZipFile(directory / "framefx.xsa") as archive:
        matches = [name for name in archive.namelist() if Path(name).name == "framefx.hwh"]
        if len(matches) != 1:
            raise RuntimeError("Expected one top-level framefx hardware handoff in XSA")
        (directory / "framefx.hwh").write_bytes(archive.read(matches[0]))
    handoff = check_hwh(directory / "framefx.hwh")
    dtc = lab.ARTIFACTS / "build/guest/host/bin/dtc"
    overlay_tool = dtc.with_name("fdtoverlay")
    vendor_dtb = lab.ARTIFACTS / "hardware/vendor-system.dtb"
    lab.verify_archive(vendor_dtb, PLATFORM["vendor_dtb_sha256"])
    for name in ("framefx", "framefx-afi"):
        lab.logged([dtc, "-@", "-I", "dts", "-O", "dtb", "-o", directory / (name + ".dtbo"),
                    DIRECTORY / (name + ".dtso")], directory / (name + "-dtc.log"), timeout=30)
    lab.logged([overlay_tool, "-i", vendor_dtb, "-o", directory / "vendor-with-framefx.dtb",
                directory / "framefx-afi.dtbo", directory / "framefx.dtbo"],
               directory / "overlay-apply.log", timeout=30)
    device_tree = check_device_tree(directory / "vendor-with-framefx.dtb")
    (directory / "framefx.bif").write_text("all:\n{\n  framefx.bit\n}\n")
    bootgen = Path(lab.vivado_path()).with_name("bootgen")
    lab.logged([bootgen, "-arch", "zynqmp", "-image", "framefx.bif", "-process_bitstream", "bin", "-w", "on"],
               directory / "bootgen.log", cwd=directory, timeout=60)
    binary = directory / "framefx.bit.bin"
    if not binary.is_file() or binary.stat().st_size != header["payload_bytes"]:
        raise RuntimeError("Unexpected Bootgen raw bitstream size")
    words = array.array("I", (directory / "framefx.bit").read_bytes()[-header["payload_bytes"]:])
    if words.itemsize != 4:
        raise RuntimeError("Host lacks 32-bit array words")
    words.byteswap()
    if binary.read_bytes() != words.tobytes():
        raise RuntimeError("Bootgen payload differs from byte-swapped Vivado bitstream")
    shutil.copyfile(DIRECTORY / "platform.json", directory / "platform.json")
    artifacts = ["framefx.bit", "framefx.bit.bin", "framefx.hwh", "framefx.xsa", "platform.json",
                 "framefx.dtbo", "framefx-afi.dtbo", "timing.rpt", "check-timing.rpt", "drc.rpt",
                 "utilization.rpt", "route-status.rpt", "ps-properties.txt", "address-map.txt", "vivado.log"]
    return {"bitstream": header, "handoff": handoff, "device_tree": device_tree,
            "artifact_sha256": {name: lab.sha256(directory / name) for name in artifacts}}


def evidence_current(record):
    try:
        directory = (lab.ROOT / record["directory"]).resolve()
        return (directory.is_relative_to((lab.ARTIFACTS / "build").resolve())
                and record["status"] == "PASS" and record["test"] == "ultra96-pl-build"
                and record["source_sha256"] == sources()
                and set(record["artifact_sha256"]) == {"framefx.bit", "framefx.bit.bin", "framefx.hwh", "framefx.xsa",
                    "platform.json", "framefx.dtbo", "framefx-afi.dtbo", "timing.rpt", "check-timing.rpt", "drc.rpt",
                    "utilization.rpt", "route-status.rpt", "ps-properties.txt", "address-map.txt", "vivado.log"}
                and all(lab.sha256(directory / name) == value for name, value in record["artifact_sha256"].items()))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def build(stage="bitstream"):
    if stage not in ("validate", "bitstream"):
        raise ValueError("Invalid PL stage")
    if not lab.vivado_path():
        raise RuntimeError("Vivado 2026.1 is required")
    repository = lab.SOURCES / "avnet-bdf-ultra96v1"
    commit = subprocess.check_output(["git", "-C", repository, "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", repository, "status", "--porcelain"], text=True)
    if commit != PLATFORM["board_commit"] or dirty:
        raise RuntimeError("V1 board repository must be clean at the reviewed commit")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    directory = lab.ARTIFACTS / "build" / ("ultra96-pl-" + stamp)
    directory.mkdir(parents=True)
    record = dict(schema=1, test="ultra96-pl-build", status="FAIL", stage=stage, hardware_tested=False,
                  directory=str(directory.relative_to(lab.ROOT)), time_utc=lab.timestamp(), source_sha256=sources())
    try:
        lab.logged([lab.vivado_path(), "-mode", "batch", "-nojournal", "-nolog", "-source",
                    DIRECTORY / "create_project.tcl", "-tclargs", lab.ROOT, stage],
                   directory / "vivado.log", cwd=directory, timeout=3600)
        if stage == "bitstream":
            record.update(package(directory, record["source_sha256"]))
        elif not re.search(r"^FRAMEFX_BD_VALIDATED xczu3eg-sbva484-1-e$",
                           (directory / "vivado.log").read_text(), re.MULTILINE):
            raise RuntimeError("Vivado validation marker missing")
        record["status"] = "PASS"
    except Exception as error:
        record["error"] = str(error)
        raise
    finally:
        lab.write_json(directory / "manifest.json", record)
        lab.write_json(lab.ARTIFACTS / ("ultra96-pl-" + stage + ".json"), record)
    print(f"PASS: {stage} only; no hardware loaded. Evidence: {directory}")


def test_wrapper(jobs):
    directory = lab.ARTIFACTS / "build/ultra96-wrapper"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "framefx-test"
    wrapper = lab.ROOT / "rtl/board/framefx_ultra96.sv"
    lab.logged(["verilator", "--cc", "--exe", "--build", "--assert", "--trace", "--timescale", "1ns/1ps",
                "--Wall", "--Mdir", directory, "--top-module", "framefx_ultra96", "--prefix", "Vframefx_core",
                "-o", binary.name, "-CFLAGS", "-std=c++17", "-j", str(jobs), wrapper,
                *framefx.SOURCES, lab.ROOT / "sim/framefx_test.cpp"],
               lab.ARTIFACTS / "logs/ultra96-wrapper-build.log", timeout=180)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    evidence = lab.ARTIFACTS / "hardware" / ("wrapper-" + stamp)
    evidence.mkdir(parents=True)
    record = dict(schema=1, test="ultra96-wrapper-simulation", status="FAIL", hardware_tested=False,
                  source_sha256=sources(), binary_sha256=lab.sha256(binary), cases=0,
                  directory=str(evidence.relative_to(lab.ROOT)))
    try:
        for case in framefx.cases():
            framefx.run_case(evidence / f"case-{case['id']}", binary, case, 2026, 16)
            record["cases"] += 1
        if record["source_sha256"] != sources():
            raise RuntimeError("Sources changed during wrapper regression")
        record["status"] = "PASS"
    finally:
        lab.write_json(evidence / "manifest.json", record)
        lab.write_json(lab.ARTIFACTS / "ultra96-wrapper.json", record)
    print(f"PASS: {record['cases']} board-wrapper RTL cases, not physical hardware")
