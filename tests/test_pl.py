import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pl

SPEC = importlib.util.spec_from_file_location("pl_load", ROOT / "linux/hardware/ultra96-v1/pl_load.py")
pl_load = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pl_load)


def bitstream(part="xczu3eg-sbva484-1-e"):
    output = b"\x00\x09" + bytes.fromhex("0ff00ff00ff00ff000") + b"\x00\x01"
    for field, value in ((b"a", "framefx"), (b"b", part), (b"c", "2026/10/16"), (b"d", "12:00:00")):
        encoded = value.encode() + b"\0"
        output += field + struct.pack(">H", len(encoded)) + encoded
    payload = b"\xff" * 64 + b"\xaa\x99\x55\x66" + b"\0" * 188
    return output + b"e" + struct.pack(">I", len(payload)) + payload


def handoff():
    root = ET.Element("EDKSYSTEM")
    ET.SubElement(root, "SYSTEMINFO", DEVICE="xczu3eg", PACKAGE="sbva484", BOARD=pl.PLATFORM["board_part"])
    modules = ET.SubElement(root, "MODULES")
    parameters = {
        "ps": {"PSU__DDRC__MEMORY_TYPE": "LPDDR 4", "PSU__DDRC__BUS_WIDTH": "32 Bit",
               "PSU__DDRC__DEVICE_CAPACITY": "8192 MBits", "PSU__DDRC__T_RC": "64",
               "PSU__USE__M_AXI_GP0": "0", "PSU__USE__M_AXI_GP1": "0", "PSU__USE__M_AXI_GP2": "1",
               "PSU__MAXIGP2__DATA_WIDTH": "32", "PSU__USE__S_AXI_GP2": "1",
               "PSU__SAXIGP2__DATA_WIDTH": "128", "PSU__USE__IRQ0": "1",
               "PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ": "100", "PSU__FPGA_PL0_ENABLE": "1",
               "PSU__SD1__PERIPHERAL__IO": "MIO 46 .. 51", "PSU__USB0__PERIPHERAL__IO": "MIO 52 .. 63",
               "PSU__USB1__PERIPHERAL__IO": "MIO 64 .. 75", "PSU__DPAUX__PERIPHERAL__IO": "MIO 27 .. 30"},
        "reset": {"C_EXT_RESET_HIGH": "0", "C_AUX_RESET_HIGH": "0"},
        "one": {"CONST_VAL": "0x1"}, "zero": {"CONST_VAL": "0x0"}}
    instances = {}
    for name in ("ps", "accelerator", "control", "memory", "reset", "one", "zero", "interrupts"):
        instances[name] = ET.SubElement(modules, "MODULE", INSTANCE=name,
                                       MODTYPE="zynq_ultra_ps_e" if name == "ps" else name)
        values = ET.SubElement(instances[name], "PARAMETERS")
        for key, value in parameters.get(name, {}).items():
            ET.SubElement(values, "PARAMETER", NAME=key, VALUE=value)
        ET.SubElement(instances[name], "PORTS")
        ET.SubElement(instances[name], "BUSINTERFACES")
    control = ET.SubElement(instances["ps"], "MEMORYMAP")
    ET.SubElement(control, "MEMRANGE", INSTANCE="accelerator", BASEVALUE="0x80000000", HIGHVALUE="0x80000fff",
                  MASTERBUSINTERFACE="M_AXI_HPM0_LPD")
    memory = ET.SubElement(instances["accelerator"], "MEMORYMAP")
    ET.SubElement(memory, "MEMRANGE", BASEVALUE="0x0", HIGHVALUE="0x7fffffff", SLAVEBUSINTERFACE="S_AXI_HP0_FPD")
    connections = [
        ["ps/pl_clk0", "ps/maxihpm0_lpd_aclk", "ps/saxihp0_fpd_aclk", "accelerator/clk",
         "control/aclk", "memory/aclk", "reset/slowest_sync_clk"],
        ["ps/pl_resetn0", "reset/ext_reset_in"], ["one/dout", "reset/aux_reset_in", "reset/dcm_locked"],
        ["zero/dout", "reset/mb_debug_sys_rst", *[f"interrupts/In{index}" for index in range(1, 8)]],
        ["reset/peripheral_aresetn", "accelerator/reset_n", "control/aresetn", "memory/aresetn"],
        ["accelerator/irq", "interrupts/In0"], ["interrupts/dout", "ps/pl_ps_irq0"]]
    for index, endpoints in enumerate(connections):
        for endpoint in endpoints:
            module, name = endpoint.split("/")
            ET.SubElement(instances[module].find("PORTS"), "PORT", NAME=name, SIGNAME=f"net{index}")
    buses = [("ps/M_AXI_HPM0_LPD", "control/S00_AXI"), ("control/M00_AXI", "accelerator/S_AXI"),
             ("accelerator/M_AXI", "memory/S00_AXI"), ("memory/M00_AXI", "ps/S_AXI_HP0_FPD")]
    for index, endpoints in enumerate(buses):
        for endpoint in endpoints:
            module, name = endpoint.split("/")
            ET.SubElement(instances[module].find("BUSINTERFACES"), "BUSINTERFACE", NAME=name, BUSNAME=f"bus{index}")
    return root


class PlBuildTests(unittest.TestCase):
    def test_device_tree_requires_direct_child_and_reviewed_resources(self):
        region = "/fpga-full"
        device = region + "/framefx@80000000"
        properties = {(region, "#address-cells"): "2", (region, "#size-cells"): "2",
                      (region, "dma-ranges"): "0 0 0 0 0 80000000",
                      (region, "compatible"): "fpga-region",
                      (device, "reg"): "0 80000000 0 1000", (device, "interrupts"): "0 59 4",
                      (device, "interrupt-parent"): "4", (device, "clocks"): "3 47",
                      (device, "assigned-clocks"): "3 47", (device, "assigned-clock-rates"): "5f5e100",
                      (device, "compatible"): "example,axi-framefx-1.0", (device, "status"): "okay",
                      ("/axi-lab-afi", "config-afi"): "4 0 5 0 f 0",
                      ("/axi-lab-afi", "compatible"): "xlnx,afi-fpga",
                      ("/__symbols__", "zynqmp_clk"): "/clock", ("/clock", "phandle"): "3",
                      ("/__symbols__", "gic"): "/gic", ("/gic", "phandle"): "4"}
        children = "framefx@80000000"
        extra_property = ""

        def query(command, **kwargs):
            if command[1] == "-l":
                return children
            if command[1] == "-p":
                return extra_property
            return properties[tuple(command[-2:])]

        with mock.patch.object(pl.subprocess, "check_output", side_effect=query):
            self.assertTrue(pl.check_device_tree(Path("tree.dtb"))["direct_region_child"])
            for key in ((region, "dma-ranges"), (device, "reg"), (device, "interrupts"),
                        (device, "clocks"), (device, "assigned-clock-rates"), ("/axi-lab-afi", "config-afi")):
                original = properties[key]
                properties[key] = "0"
                with self.subTest(property=key), self.assertRaises(ValueError):
                    pl.check_device_tree(Path("tree.dtb"))
                properties[key] = original
            children = "framefx_bus"
            with self.assertRaises(ValueError):
                pl.check_device_tree(Path("tree.dtb"))
            children = "framefx@80000000"
            extra_property = "dma-coherent"
            with self.assertRaises(ValueError):
                pl.check_device_tree(Path("tree.dtb"))

    def test_bitstream_header_enforces_part_and_complete_payload(self):
        valid = bitstream()
        self.assertEqual(pl.bit_header(valid)["payload_bytes"], 256)
        self.assertEqual(pl.bit_header(bitstream("zu3eg-sbva484-1-e"))["part"], "zu3eg-sbva484-1-e")
        for invalid in (b"", valid[:-1], valid + b"\0", b"x" + valid[1:],
                        bitstream("xczu7ev-ffvc1156-2-e"), valid.replace(b"\xaa\x99\x55\x66", b"\0" * 4)):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                pl.bit_header(invalid)

    def test_handoff_checks_reset_clock_irq_address_and_noncoherent_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.hwh"
            ET.ElementTree(handoff()).write(path)
            self.assertEqual(pl.check_hwh(path)["clock_reset_irq_connections"], "PASS")
            mutations = [
                ("./MODULES/MODULE[@INSTANCE='reset']/PORTS/PORT[@NAME='aux_reset_in']", "SIGNAME", "net3"),
                ("./MODULES/MODULE[@INSTANCE='reset']/PARAMETERS/PARAMETER[@NAME='C_EXT_RESET_HIGH']", "VALUE", "1"),
                ("./MODULES/MODULE[@INSTANCE='accelerator']/PORTS/PORT[@NAME='clk']", "SIGNAME", "wrong_clock"),
                ("./MODULES/MODULE[@INSTANCE='interrupts']/PORTS/PORT[@NAME='In0']", "SIGNAME", "wrong_irq"),
                ("./MODULES/MODULE[@INSTANCE='ps']/MEMORYMAP/MEMRANGE", "BASEVALUE", "0xa0000000"),
                ("./MODULES/MODULE[@INSTANCE='accelerator']/MEMORYMAP/MEMRANGE", "HIGHVALUE", "0xffffffff"),
                ("./MODULES/MODULE[@INSTANCE='accelerator']/MEMORYMAP/MEMRANGE", "SLAVEBUSINTERFACE", "S_AXI_HPC0_FPD"),
                ("./MODULES/MODULE[@INSTANCE='memory']/BUSINTERFACES/BUSINTERFACE[@NAME='M00_AXI']", "BUSNAME", "wrong_bus")]
            for selector, attribute, value in mutations:
                with self.subTest(selector=selector), self.assertRaises(ValueError):
                    tree = handoff()
                    tree.find(selector).set(attribute, value)
                    ET.ElementTree(tree).write(path)
                    pl.check_hwh(path)

    def test_handoff_rejects_wrong_board_and_external_pins(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.hwh"
            for field in ("DEVICE", "PACKAGE", "BOARD", "external"):
                tree = handoff()
                if field == "external":
                    ET.SubElement(ET.SubElement(tree, "EXTERNALPORTS"), "PORT", NAME="unsafe_pin")
                else:
                    tree.find("SYSTEMINFO").set(field, "wrong")
                ET.ElementTree(tree).write(path)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    pl.check_hwh(path)

    def test_evidence_does_not_accept_missing_or_stale_artifacts(self):
        for record in ({}, {"directory": "/etc", "status": "PASS", "test": "ultra96-pl-build"},
                       {"directory": "artifacts/build/test", "status": "FAIL", "test": "ultra96-pl-build"}):
            self.assertFalse(pl.evidence_current(record))


class PlLoaderTests(unittest.TestCase):
    def test_interrupt_preflight_rejects_vendor_placeholder_and_other_owners(self):
        timer = "  11:  12136 5746 9099 8195  GICv2  30 Level arch_timer\n"
        pl_load.check_interrupts(timer, 121)
        pl_load.check_interrupts(timer.replace("GICv2", "GICv3"), 121)
        for owner in ("fabric", "framefx", "unreviewed-device", "shared-first, shared-second"):
            with self.subTest(owner=owner), self.assertRaisesRegex(RuntimeError, "IRQ collision"):
                pl_load.check_interrupts(timer + "  53:  0 0 0 0 GICv2 121 Level " + owner + "\n", 121)
        for unavailable in ("", "CPU0 CPU1 CPU2 CPU3\n", "malformed GICv2 121 fabric"):
            with self.subTest(unavailable=unavailable), self.assertRaisesRegex(RuntimeError, "unavailable"):
                pl_load.check_interrupts(unavailable, 121)

    def test_device_must_exist_at_expected_node_and_bind_before_csr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device = root / "sys/bus/platform/devices/80000000.framefx"
            node = root / "proc/device-tree/fpga-full/framefx@80000000"
            with mock.patch.object(pl_load, "Path", side_effect=lambda name: root / str(name).lstrip("/")):
                with self.assertRaises(RuntimeError):
                    pl_load.check_device()
                device.mkdir(parents=True)
                node.mkdir(parents=True)
                device.joinpath("of_node").symlink_to(node)
                node.joinpath("compatible").write_bytes(b"example,axi-framefx-1.0\0")
                pl_load.check_device()
                with self.assertRaises(RuntimeError):
                    pl_load.check_device(bound=True)
                driver = root / "sys/bus/platform/drivers/framefx"
                driver.mkdir(parents=True)
                device.joinpath("driver").symlink_to(driver)
                character = root / "dev/framefx0"
                character.parent.mkdir()
                character.write_bytes(b"")
                with self.assertRaises(RuntimeError):
                    pl_load.check_device(bound=True)
                character.unlink()
                character.symlink_to("/dev/null")
                pl_load.check_device(bound=True)
                node.joinpath("compatible").write_bytes(b"other-device\0")
                with self.assertRaises(RuntimeError):
                    pl_load.check_device()

    def test_preflight_rejects_used_pl_collisions_unhealthy_kernel_and_wrong_boot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {"/proc/device-tree/compatible": b"avnet,ultra96-rev1\0",
                     "/boot/BOOT.BIN": b"boot", "/boot/image.ub": b"image",
                     "/sys/class/fpga_manager/fpga0/name": b"Xilinx ZynqMP FPGA Manager\n",
                     "/sys/class/fpga_manager/fpga0/state": b"unknown\n",
                     "/proc/device-tree/axi/smmu@fd800000/status": b"disabled\0",
                     "/proc/meminfo": b"MemAvailable: 1000000 kB\nCmaFree: 128000 kB\n",
                     "/proc/iomem": b"00000000-7fffffff : System RAM\n",
                     "/proc/interrupts": b"  11: 1 2 3 4 GICv2 30 Level arch_timer\n",
                     "/proc/sys/kernel/random/boot_id": b"boot-id\n"}
            for name, data in files.items():
                path = root / name.lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            for name in ("sys/kernel/config/device-tree/overlays", "proc/device-tree/fpga-full"):
                (root / name).mkdir(parents=True)
            settings = {**pl.PLATFORM, "boot_sha256": pl_load.digest(b"boot"), "image_ub_sha256": pl_load.digest(b"image")}
            with mock.patch.object(pl_load, "Path", side_effect=lambda name: root / str(name).lstrip("/")), \
                 mock.patch.object(pl_load, "MANAGER", root / "sys/class/fpga_manager/fpga0"), \
                 mock.patch.object(pl_load, "OVERLAYS", root / "sys/kernel/config/device-tree/overlays"), \
                 mock.patch.object(pl_load.os, "geteuid", return_value=0), \
                 mock.patch.object(pl_load.platform, "release", return_value=settings["kernel"]), \
                 mock.patch.object(pl_load.subprocess, "check_output", return_value="healthy") as messages:
                self.assertEqual(pl_load.preflight(settings)["boot_id"], "boot-id")
                messages.return_value = "[    2.394156] mtdoops: mtd device (mtddev=name/number) must be supplied"
                self.assertEqual(pl_load.preflight(settings)["boot_id"], "boot-id")
                for name, bad in (("/sys/class/fpga_manager/fpga0/state", b"operating\n"),
                                  ("/proc/iomem", b"80000000-80000fff : other-device\n"),
                                  ("/proc/interrupts", b"  53: 0 0 0 0 GICv2 121 Level fabric\n"),
                                  ("/boot/BOOT.BIN", b"changed"),
                                  ("/proc/device-tree/compatible", b"avnet,ultra96-rev2\0"),
                                  ("/proc/device-tree/axi/smmu@fd800000/status", b"okay\0"),
                                  ("/proc/meminfo", b"MemAvailable: 1000000 kB\nCmaFree: 0 kB\n")):
                    path = root / name.lstrip("/")
                    path.write_bytes(bad)
                    with self.subTest(name=name), self.assertRaises(RuntimeError):
                        pl_load.preflight(settings)
                    path.write_bytes(files[name])
                existing = root / "sys/kernel/config/device-tree/overlays/existing"
                existing.mkdir()
                with self.assertRaises(RuntimeError):
                    pl_load.preflight(settings)
                existing.rmdir()
                for diagnostic in ("rcu: INFO: rcu_sched detected stalls on CPUs/tasks:",
                                   "rcu: rcu_preempt kthread starved for 100 jiffies!",
                                   "[   12.000000] Oops: 96000004 [#1] SMP",
                                   "[   12.000000] Internal error: Oops: 96000004",
                                   "[   12.000000] SError Interrupt on CPU0",
                                   "[   12.000000] Kernel panic - not syncing: Fatal exception"):
                    messages.return_value = diagnostic
                    with self.subTest(diagnostic=diagnostic), self.assertRaises(RuntimeError):
                        pl_load.preflight(settings)

    def test_explicit_confirmation_precedes_any_hardware_access(self):
        with mock.patch.object(pl_load, "preflight") as preflight:
            for confirmation in ("", "yes", "LOAD", pl_load.CONFIRMATION.lower()):
                with self.subTest(confirmation=confirmation), self.assertRaises(ValueError):
                    pl_load.load({}, {}, confirmation)
            preflight.assert_not_called()

    def test_bundle_requires_exact_files_hashes_and_board(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            files = {}
            for name in pl_load.FILES:
                data = json.dumps(pl.PLATFORM).encode() if name == "platform.json" else b"payload"
                (directory / name).write_bytes(data)
                files[name] = pl_load.digest(data)
            manifest = dict(schema=1, files=files)
            (directory / "load-manifest.json").write_text(json.dumps(manifest))
            self.assertEqual(set(pl_load.checked_bundle(directory)[0]), pl_load.FILES)
            (directory / "framefx.ko").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                pl_load.checked_bundle(directory)
            (directory / "framefx.ko").unlink()
            (directory / "framefx.ko").symlink_to(directory / "framefx-test")
            with self.assertRaises(ValueError):
                pl_load.checked_bundle(directory)
            manifest["files"]["../escape"] = "hash"
            (directory / "load-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                pl_load.checked_bundle(directory)

    def test_loader_has_no_fpga_status_read_or_raw_memory_access(self):
        source = (ROOT / "linux/hardware/ultra96-v1/pl_load.py").read_text()
        self.assertNotIn('joinpath("status")', source)
        self.assertNotIn("/dev/mem", source)
        self.assertNotIn('"rmmod"', source)
        self.assertNotIn('"modprobe"', source)
        self.assertNotIn('"--mode"', source)

    def test_clock_lifetime_tracks_quarantine_not_devm_removal(self):
        source = (ROOT / "linux/framefx/driver/framefx.c").read_text()
        destroy = source.split("static void fx_destroy", 1)[1].split("static void fx_free", 1)[0]
        probe = source.split("static int fx_probe", 1)[1]
        self.assertIn("clk_disable_unprepare(device->clock)", destroy)
        self.assertIn("clk_put(device->clock)", destroy)
        self.assertLess(probe.index("clk_prepare_enable"), probe.index("devm_platform_ioremap_resource"))
        self.assertNotIn("devm_clk", source)


if __name__ == "__main__":
    unittest.main()
