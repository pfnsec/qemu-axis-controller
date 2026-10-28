from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time

from bridge_probe import HEADER, receive, send
import lab
import spike


MAGIC = 0x46524658
MMIO = 0x0c000000
CASES = ("bus-errors", "reply-id", "reply-address", "reply-kind", "short-read", "long-read",
         "error-payload", "unexpected-status", "reserved", "version", "oversized", "disconnect",
         "partial-header", "slow-header", "reverse-flood", "reverse-replay", "reverse-response",
         "irq-level", "irq-address", "irq-length", "reverse-kind")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


@contextmanager
def control(runtime, name):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(runtime / f"{name}.sock"))
        with connection.makefile("rwb", buffering=0) as stream:
            yield stream


def qmp(stream, execute):
    stream.write(json.dumps({"execute": execute}).encode() + b"\n")
    while True:
        line = stream.readline()
        require(bool(line), "QMP disconnected")
        response = json.loads(line)
        if "event" not in response:
            require("return" in response, f"QMP {execute}: {response}")
            return response["return"]


def access(stream, operation, address, value=None):
    command = f"{operation} {address:#x}" + (f" {value:#x}" if value is not None else "")
    stream.write(command.encode() + b"\n")
    response = stream.readline().decode().strip()
    require(response.startswith("OK"), f"QTest {command}: {response}")
    return int(response.split()[1], 0) if operation == "readl" else None


def packet(kind, request_id, address=0, payload=b"", status=0):
    return HEADER.pack(0x41584943, 1, 0, kind, 0, status, request_id, address, len(payload), 0) + payload


def command_line(runtime, timeout=150):
    return [str(lab.ARTIFACTS / "build/qemu/qemu-system-aarch64"), "-machine",
            "virt-10.2,gic-version=3,dtb-randomness=off", "-accel", "tcg,thread=single",
            "-cpu", "cortex-a53", "-smp", "1", "-m", "512M", "-S", "-display", "none",
            "-serial", "none", "-monitor", "none", "-nic", "none",
            "-qmp", f"unix:{runtime}/qmp.sock,server=on,wait=off",
            "-qtest", f"unix:{runtime}/qtest.sock,server=on,wait=off", "-qtest-log", "/dev/null",
            "-device", f"axi-cosim,command={runtime}/command.sock,reverse={runtime}/reverse.sock,session=123,timeout-ms={timeout},spike=off"]


def run_case(evidence, scenario):
    require(scenario in CASES, "Unknown adversarial scenario")
    evidence.mkdir(parents=True)
    result = dict(scenario=scenario, status="FAIL", rtl_tested=False)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="axi-adversary-") as temporary:
            runtime = Path(temporary)
            listeners, channels, requests = [], [], []
            initialized = threading.Event()
            for name in ("command", "reverse"):
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(runtime / f"{name}.sock"))
                listener.listen(1)
                listener.settimeout(5)
                listeners.append(listener)

            def peer():
                try:
                    for listener in listeners:
                        stream, _ = listener.accept()
                        stream.settimeout(5)
                        channels.append(stream)
                        hello = receive(stream)
                        expected = struct.pack("<IIIIIIQ", 32, 64, 16, 1, 100000000, 128, 123)
                        require(hello == (1, 0, 1, 0, expected), "Adversarial HELLO mismatch")
                        send(stream, 0x8001, 0, 1, 0, expected)
                    command, reverse = channels
                    while True:
                        kind, status, request_id, address, payload = receive(command)
                        require(status == 0 and (not requests or request_id > requests[-1][1]), "Command ordering")
                        requests.append((kind, request_id, address))
                        if kind in (7, 9):
                            require(not address and not payload, "Control payload")
                            send(command, kind | 0x8000, 0, request_id, 0)
                            initialized.set()
                            if kind == 9:
                                require(command.recv(1) == b"", "GOODBYE closure")
                                return
                            continue
                        require(kind in (2, 3), "Unexpected command")
                        if scenario == "bus-errors":
                            bus_error = address == 0x100
                            response = struct.pack("<I", MAGIC) if kind == 2 and not bus_error else b""
                            send(command, kind | 0x8000, 7 if bus_error else 0, request_id, address, response)
                            continue
                        response = packet(0x8002, request_id, address, struct.pack("<I", MAGIC))
                        if scenario == "reply-id":
                            response = packet(0x8002, request_id + 1, address, struct.pack("<I", MAGIC))
                        elif scenario == "reply-address":
                            response = packet(0x8002, request_id, address + 4, struct.pack("<I", MAGIC))
                        elif scenario == "reply-kind":
                            response = packet(0x8003, request_id, address)
                        elif scenario in ("short-read", "long-read"):
                            response = packet(0x8002, request_id, address, bytes(3 if scenario == "short-read" else 8))
                        elif scenario == "error-payload":
                            response = packet(0x8002, request_id, address, bytes(4), status=7)
                        elif scenario == "unexpected-status":
                            response = packet(0x8002, request_id, address, status=5)
                        elif scenario in ("reserved", "version", "oversized"):
                            response = bytearray(response)
                            if scenario == "reserved":
                                response[10] = 1
                            elif scenario == "version":
                                response[4] = 2
                            else:
                                response[32:36] = struct.pack("<I", 1025)
                        elif scenario == "disconnect":
                            command.shutdown(socket.SHUT_RDWR)
                            return
                        elif scenario == "partial-header":
                            command.sendall(response[:11])
                            command.shutdown(socket.SHUT_RDWR)
                            return
                        elif scenario == "slow-header":
                            for value in response:
                                command.sendall(bytes([value]))
                                time.sleep(0.04)
                            raise RuntimeError("Slow sender escaped absolute deadline")
                        elif scenario == "reverse-flood":
                            for ordinal in range(2, 100002):
                                reverse.sendall(packet(6, ordinal, payload=bytes(4)))
                            continue
                        elif scenario.startswith("reverse-") or scenario.startswith("irq-"):
                            kind = 0x8006 if scenario == "reverse-response" else 2 if scenario == "reverse-kind" else 6
                            reverse.sendall(packet(kind, 1 if scenario == "reverse-replay" else 2,
                                                   4 if scenario == "irq-address" else 0,
                                                   bytes(3) if scenario == "irq-length" else struct.pack("<I", 2 if scenario == "irq-level" else 0)))
                            continue
                        command.sendall(response)
                except (BrokenPipeError, ConnectionResetError):
                    if scenario == "bus-errors":
                        raise
                except RuntimeError as error:
                    if scenario == "bus-errors" or str(error) != "Probe peer disconnected":
                        raise

            command = command_line(runtime)
            lab.write_json(evidence / "command.json", command)
            with (evidence / "qemu.log").open("w") as output, ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(peer)
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                           env=lab.build_environment(), start_new_session=True)
                try:
                    require(initialized.wait(5), "Adversarial startup timeout")
                    with control(runtime, "qmp") as monitor, control(runtime, "qtest") as tester:
                        require("QMP" in json.loads(monitor.readline()), "QMP greeting")
                        qmp(monitor, "qmp_capabilities")
                        if scenario == "bus-errors":
                            for operation in ("readl", "writel") * 8:
                                require(access(tester, "readl", MMIO) == MAGIC, "Bus error poisoned bridge")
                                access(tester, operation, MMIO + 0x100, 1 if operation == "writel" else None)
                            require(access(tester, "readl", MMIO) == MAGIC, "Bus error poisoned bridge")
                        else:
                            fault_started = time.monotonic()
                            require(access(tester, "readl", MMIO) == 0xffffffff, "Fault read did not fail closed")
                            result["fault_seconds"] = round(time.monotonic() - fault_started, 4)
                            require(result["fault_seconds"] < 2, "Fault exceeded bounded deadline")
                            count = len(requests)
                            qmp(monitor, "system_reset")
                            for _ in range(4):
                                require(access(tester, "readl", MMIO) == 0xffffffff, "Failed session revived")
                                access(tester, "writel", MMIO + 0x0c, 1)
                            require(len(requests) == count, "Post-fault MMIO/reset reached peer")
                        qmp(monitor, "query-status")
                        qmp(monitor, "quit")
                    require(process.wait(timeout=5) == 0, "Adversarial QEMU exit")
                    future.result(timeout=5)
                finally:
                    spike.stop(process)
                    for stream in channels:
                        stream.close()
                    for listener in listeners:
                        listener.close()
            log = (evidence / "qemu.log").read_text()
            faults = log.count("AXI_COSIM_FAULT")
            require(faults == (0 if scenario == "bus-errors" else 1), "Incorrect terminal fault count")
            if scenario in ("slow-header", "reverse-flood"):
                require("error=-110" in log, "Absolute deadline was not enforced")
            result.update(status="PASS", terminal_faults=faults, requests=len(requests))
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        lab.write_json(evidence / "result.json", result)
        print(f"{result['status']}: {evidence.relative_to(lab.ROOT)}", flush=True)
    return result


def run_rtl_case(evidence):
    evidence.mkdir(parents=True)
    result = dict(scenario="rtl-bus-errors", status="FAIL", rtl_tested=True, invalid_reads=0, invalid_writes=0)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="axi-rtl-probe-") as temporary:
            runtime = Path(temporary)
            simulator_command = [str(lab.ARTIFACTS / "build/framefx-socket/Vframefx_core"), str(runtime), "123", "42", "3", "none"]
            command = command_line(runtime, timeout=2000)
            lab.write_json(evidence / "commands.json", dict(qemu=command, simulator=simulator_command))
            with (evidence / "simulator.log").open("w") as simulator_log, (evidence / "qemu.log").open("w") as qemu_log:
                simulator = subprocess.Popen(simulator_command, stdout=simulator_log, stderr=subprocess.STDOUT,
                                             env=lab.build_environment(), start_new_session=True)
                process = None
                try:
                    deadline = time.monotonic() + 5
                    while "RTL_LISTENING" not in (evidence / "simulator.log").read_text():
                        require(simulator.poll() is None and time.monotonic() < deadline, "RTL probe startup")
                        time.sleep(0.01)
                    process = subprocess.Popen(command, stdout=qemu_log, stderr=subprocess.STDOUT,
                                               env=lab.build_environment(), start_new_session=True)
                    deadline = time.monotonic() + 5
                    while not (runtime / "qtest.sock").exists():
                        require(process.poll() is None and time.monotonic() < deadline, "QTest probe startup")
                        time.sleep(0.01)
                    with control(runtime, "qmp") as monitor, control(runtime, "qtest") as tester:
                        require("QMP" in json.loads(monitor.readline()), "QMP greeting")
                        qmp(monitor, "qmp_capabilities")
                        for iteration in range(32):
                            for offset in (0x0c, 0x058, 0x080, 0xffc):
                                require(access(tester, "readl", MMIO + offset) == 0xffffffff, "Invalid RTL read was accepted")
                                require(access(tester, "readl", MMIO) == MAGIC, "RTL SLVERR poisoned bridge")
                                result["invalid_reads"] += 1
                            for offset in (0, 4, 8, 0x10, 0x44, 0x48, 0x50, 0xffc):
                                access(tester, "writel", MMIO + offset, 0xdeadbeef)
                                require(access(tester, "readl", MMIO) == MAGIC, "RTL write SLVERR poisoned bridge")
                                result["invalid_writes"] += 1
                            access(tester, "writel", MMIO + 0x38, iteration)
                            require(access(tester, "readl", MMIO + 0x38) == iteration, "Valid RTL MMIO stopped working")
                        qmp(monitor, "system_reset")
                        require(access(tester, "readl", MMIO) == MAGIC, "Healthy RTL reset failed")
                        qmp(monitor, "quit")
                    require(process.wait(timeout=5) == 0 and simulator.wait(timeout=5) == 0, "RTL probe clean shutdown")
                finally:
                    if process is not None:
                        spike.stop(process)
                    spike.stop(simulator)
            require("AXI_COSIM_FAULT" not in (evidence / "qemu.log").read_text(), "RTL probe bridge fault")
            require("RTL_SUMMARY" in (evidence / "simulator.log").read_text(), "RTL probe summary missing")
            result["status"] = "PASS"
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        lab.write_json(evidence / "result.json", result)
        print(f"{result['status']}: {evidence.relative_to(lab.ROOT)}", flush=True)
    return result


def test(evidence):
    return [run_case(evidence / scenario, scenario) for scenario in CASES] + [run_rtl_case(evidence / "rtl-bus-errors")]
