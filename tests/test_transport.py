from concurrent.futures import ThreadPoolExecutor
import ctypes
import errno
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HEADER = struct.Struct("<IHHHHIQQII")


def frame(kind=2, status=0, request_id=1, address=0, payload=b""):
    return HEADER.pack(0x41584943, 1, 0, kind, 0, status, request_id, address, len(payload), 0) + payload


def receive_exact(stream, count):
    received = bytearray()
    while len(received) < count:
        fragment = stream.recv(count - len(received))
        if not fragment:
            raise RuntimeError("unexpected EOF")
        received.extend(fragment)
    return bytes(received)


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.library_path = Path(cls.temporary.name) / "wire.so"
        subprocess.run(["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        "-I", str(ROOT / "transport"), str(ROOT / "tests/fixtures/wire_test.c"),
                        "-o", str(cls.library_path)], check=True)
        cls.library = ctypes.CDLL(str(cls.library_path))
        cls.library.test_encode.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint32,
                                           ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32]
        cls.library.test_decode.argtypes = [ctypes.c_void_p]
        cls.library.test_receive.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        cls.library.test_send.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
        cls.library.test_pump.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        cls.library.test_match.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cls.library.test_interrupted_receive.argtypes = [ctypes.c_int]

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_independent_little_endian_golden_header(self):
        output = ctypes.create_string_buffer(40)
        self.assertEqual(self.library.test_encode(output, 0x8004, 2, 0x0102030405060708, 0x1122334455667788, 8), 0)
        expected = bytes.fromhex("43495841 0100 0000 0480 0000 02000000 0807060504030201 8877665544332211 08000000 00000000")
        self.assertEqual(output.raw, expected)
        self.assertEqual(self.library.test_decode(expected), 0)

    def test_strict_header_validation(self):
        valid = frame()
        mutations = ((0, 0), (4, 2), (6, 1), (8, 0), (8, 10), (10, 1), (12, 1), (16, 0), (36, 1))
        for offset, value in mutations:
            malformed = bytearray(valid)
            malformed[offset] = value
            with self.subTest(offset=offset):
                self.assertEqual(self.library.test_decode(bytes(malformed)), -errno.EPROTO)
        oversized = bytearray(valid)
        oversized[32:36] = struct.pack("<I", 1025)
        self.assertEqual(self.library.test_decode(bytes(oversized)), -errno.EPROTO)
        self.assertEqual(self.library.test_decode(frame(kind=0x8002, status=8)), -errno.EPROTO)

    def test_every_family_and_status(self):
        for kind in range(1, 10):
            self.assertEqual(self.library.test_decode(frame(kind=kind)), 0)
            for status in range(8):
                self.assertEqual(self.library.test_decode(frame(kind=kind | 0x8000, status=status)), 0)

    def test_one_byte_reads_and_writes(self):
        packet = frame(kind=3, address=0x24, payload=b"\x12\x34\x56\x78")
        receiver, sender = socket.socketpair()
        with receiver, sender, ThreadPoolExecutor(max_workers=1) as executor:
            output = ctypes.create_string_buffer(1064)
            future = executor.submit(self.library.test_receive, receiver.fileno(), 2000, output)
            for byte in packet:
                sender.sendall(bytes([byte]))
                time.sleep(0.001)
            self.assertEqual(future.result(timeout=3), 0)
            self.assertEqual(output.raw[:len(packet)], packet)
            future = executor.submit(self.library.test_send, receiver.fileno(), 2000, 1, packet)
            self.assertEqual(receive_exact(sender, len(packet)), packet)
            self.assertEqual(future.result(timeout=3), 0)

    def test_absolute_deadline_and_midframe_disconnect(self):
        receiver, sender = socket.socketpair()
        with receiver, sender:
            output = ctypes.create_string_buffer(1064)
            sender.sendall(frame()[:10])
            started = time.monotonic()
            self.assertEqual(self.library.test_receive(receiver.fileno(), 60, output), -errno.ETIMEDOUT)
            self.assertLess(time.monotonic() - started, 1)
            sender.sendall(frame()[:5])
            sender.shutdown(socket.SHUT_WR)
            self.assertEqual(self.library.test_receive(receiver.fileno(), 200, output), -errno.ECONNRESET)

    def test_output_backpressure_has_finite_deadline(self):
        receiver, sender = socket.socketpair()
        with receiver, sender:
            sender.setblocking(False)
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
            with self.assertRaises(BlockingIOError):
                while True:
                    sender.send(b"x" * 1024)
            self.assertEqual(self.library.test_send(sender.fileno(), 50, 0, frame()), -errno.ETIMEDOUT)

    def test_reverse_traffic_completes_before_command_reply(self):
        command, command_peer = socket.socketpair()
        reverse, reverse_peer = socket.socketpair()
        with command, command_peer, reverse, reverse_peer, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.library.test_pump, command.fileno(), reverse.fileno(), 1000)
            reverse_peer.settimeout(1)
            reverse_peer.sendall(frame(kind=4, address=0x12345678, payload=struct.pack("<I", 8)))
            expected = frame(kind=0x8004, address=0x12345678, payload=struct.pack("<I", 8))
            self.assertEqual(receive_exact(reverse_peer, len(expected)), expected)
            self.assertFalse(future.done())
            command_peer.sendall(frame(kind=0x8003))
            self.assertEqual(future.result(timeout=1), 0)

    def test_reply_correlation(self):
        request = frame(kind=3, request_id=100, address=0x20)
        self.assertEqual(self.library.test_match(request, frame(kind=0x8003, request_id=100, address=0x20)), 1)
        for response in (frame(kind=3, request_id=100, address=0x20),
                         frame(kind=0x8002, request_id=100, address=0x20),
                         frame(kind=0x8003, request_id=99, address=0x20),
                         frame(kind=0x8003, request_id=100, address=0x24)):
            self.assertEqual(self.library.test_match(request, response), 0)

    def test_eintr_preserves_deadline(self):
        receiver, sender = socket.socketpair()
        with receiver, sender:
            self.assertEqual(self.library.test_interrupted_receive(receiver.fileno()), 1)


if __name__ == "__main__":
    unittest.main()
