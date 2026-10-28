from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time

import lab


HEADER = struct.Struct("<IHHHHIQQII")


def exact(stream, count):
    output = bytearray()
    while len(output) < count:
        data = stream.recv(count - len(output))
        if not data:
            raise RuntimeError("Probe peer disconnected")
        output.extend(data)
    return bytes(output)


def receive(stream):
    header = HEADER.unpack(exact(stream, HEADER.size))
    magic, major, minor, kind, reserved, status, request_id, address, length, flags = header
    if (magic, major, minor, reserved, flags) != (0x41584943, 1, 0, 0, 0) or length > 1024:
        raise RuntimeError("Probe received invalid header")
    return kind, status, request_id, address, exact(stream, length)


def send(stream, kind, status, request_id, address, payload=b""):
    stream.sendall(HEADER.pack(0x41584943, 1, 0, kind, 0, status, request_id, address, len(payload), 0) + payload)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(evidence):
    import spike
    evidence.mkdir(parents=True)
    result = {"test": "synthetic-peer-bridge-safety", "rtl_tested": False, "status": "FAIL", "checks": []}
    try:
        with tempfile.TemporaryDirectory(prefix="axi-probe-") as temporary:
            runtime = Path(temporary)
            listeners = [socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) for _ in range(2)]
            for listener, name in zip(listeners, ("command", "reverse")):
                listener.bind(str(runtime / f"{name}.sock"))
                listener.listen(1)
                listener.settimeout(5)
            initialized = threading.Event()
            connected = threading.Event()
            channels = []
            resets = []

            def peer():
                try:
                    for listener in listeners:
                        stream, _ = listener.accept()
                        stream.settimeout(10)
                        channels.append(stream)
                        kind, status, request_id, address, payload = receive(stream)
                        expected = struct.pack("<IIIIIIQ", 32, 64, 16, 1, 100000000, 128, 123)
                        require((kind, status, request_id, address, payload) == (1, 0, 1, 0, expected), "Probe HELLO mismatch")
                        send(stream, 0x8001, 0, request_id, address, payload)
                    connected.set()
                    command = channels[0]
                    previous = 1
                    while True:
                        kind, status, request_id, address, payload = receive(command)
                        require(request_id > previous and status == 0, "Probe command ordering")
                        previous = request_id
                        require(kind in (7, 9) and not address and not payload, "Unexpected probe command")
                        if kind == 7:
                            resets.append(request_id)
                        send(command, kind | 0x8000, 0, request_id, 0)
                        initialized.set()
                        if kind == 9:
                            require(command.recv(1) == b"", "Missing GOODBYE closure")
                            return
                finally:
                    for stream in channels:
                        stream.close()

            command = [str(lab.ARTIFACTS / "build/qemu/qemu-system-aarch64"), "-machine",
                       "virt-10.2,gic-version=3,dtb-randomness=off", "-accel", "tcg,thread=single",
                       "-cpu", "cortex-a53", "-smp", "1", "-m", "512M", "-S", "-display", "none",
                       "-serial", "none", "-monitor", "none", "-nic", "none",
                       "-qmp", f"unix:{runtime}/qmp.sock,server=on,wait=off", "-device",
                       f"axi-cosim,command={runtime}/command.sock,reverse={runtime}/reverse.sock,session=123"]
            lab.write_json(evidence / "command.json", command)
            with (evidence / "qemu.log").open("w") as output, ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(peer)
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                           env=lab.build_environment(), start_new_session=True)
                try:
                    require(connected.wait(5) and initialized.wait(5), "Probe startup/reset timeout")
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as monitor:
                        monitor.settimeout(5)
                        monitor.connect(str(runtime / "qmp.sock"))
                        with monitor.makefile("rwb", buffering=0) as stream:
                            require("QMP" in json.loads(stream.readline()), "QMP greeting")
                            commands = []

                            def qmp(execute, arguments=None):
                                request = {"execute": execute}
                                if arguments is not None:
                                    request["arguments"] = arguments
                                stream.write(json.dumps(request).encode() + b"\n")
                                while True:
                                    response = json.loads(stream.readline())
                                    if "event" not in response:
                                        commands.append({"request": request, "response": response})
                                        return response

                            require("return" in qmp("qmp_capabilities"), "QMP capabilities")
                            next_id = 1

                            def dma(kind, address, payload, expected_status, expected_payload=b""):
                                nonlocal next_id
                                next_id += 1
                                send(channels[1], kind, 0, next_id, address, payload)
                                response = receive(channels[1])
                                require(response == (kind | 0x8000, expected_status, next_id, address, expected_payload),
                                        f"DMA validation mismatch: {response}")
                                result["checks"].append({"kind": kind, "address": address, "status": expected_status})

                            data = bytes(range(8))
                            test_address = 0x41001000
                            dma(5, test_address, struct.pack("<I", 8) + data + b"\x01" * 8, 0)
                            dma(5, test_address, struct.pack("<I", 8) + b"\xff" * 8 + b"\x01\x00" * 4, 0)
                            expected = bytes([255, 1, 255, 3, 255, 5, 255, 7])
                            dma(4, test_address, struct.pack("<I", 8), 0, expected)
                            snapshot = evidence / "ram.bin"
                            require("return" in qmp("pmemsave", {"val": test_address, "size": 8, "filename": str(snapshot)}), "RAM dump")
                            require(snapshot.read_bytes() == expected, "Independent guest RAM visibility check")
                            for address in (0, 0x09000000, 0x0c000000, 0x60000000, 0xfffffffffffffff8):
                                dma(4, address, struct.pack("<I", 8), 2)
                                dma(5, address, struct.pack("<I", 8) + data + b"\x01" * 8, 2)
                            dma(4, test_address + 1, struct.pack("<I", 8), 1)
                            dma(4, test_address, struct.pack("<I", 7), 1)
                            for count in (0, 129, 0xffffffff):
                                dma(4, test_address, struct.pack("<I", count), 2)
                            dma(4, test_address, b"", 5)
                            dma(5, test_address, struct.pack("<I", 8) + data + b"\x02" * 8, 5)
                            dma(5, 0x5ffffff8, struct.pack("<I", 8) + data + b"\x01" * 8, 0)
                            dma(5, 0x5ffffff8, struct.pack("<I", 16) + b"\xff" * 16 + b"\x01" * 16, 2)
                            dma(4, 0x5ffffff8, struct.pack("<I", 8), 0, data)
                            dma(4, test_address, struct.pack("<I", 8), 0, expected)
                            migration = qmp("migrate", {"uri": f"file:{evidence / 'forbidden-migration'}"})
                            require("error" in migration and "non-migratable" in migration["error"]["desc"], "Migration was not blocked")
                            require("return" in qmp("system_reset"), "QMP system reset")
                            deadline = time.monotonic() + 3
                            while len(resets) < 2 and time.monotonic() < deadline:
                                time.sleep(0.01)
                            require(len(resets) == 2, "Reset did not reach peer")
                            dma(4, test_address, struct.pack("<I", 8), 0, expected)
                            require("return" in qmp("quit"), "QMP quit")
                            lab.write_json(evidence / "qmp.json", commands)
                    require(process.wait(timeout=5) == 0, "Probe QEMU failed")
                    future.result(timeout=5)
                finally:
                    spike.stop(process)
                    for listener in listeners:
                        listener.close()
            result.update({"status": "PASS", "reset_count": len(resets), "migration_blocked": True})
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        lab.write_json(evidence / "result.json", result)
        print(f"{result['status']}: {evidence.relative_to(lab.ROOT)}", flush=True)
    return result
