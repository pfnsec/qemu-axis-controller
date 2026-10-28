# AXI co-simulation wire protocol 1.0

Implementation: `transport/wire.h`, `qemu/cosim-device/axi-cosim.c`,
`sim/spike_main.cpp`, and `sim/axi/socket_memory.hpp`. This is a private local protocol, not Remote-Port.
It is not an authenticated network service. The runner creates a mode-0700
temporary directory; the simulator binds two mode-0600 Unix stream sockets.
Never expose these sockets through a network proxy.

## Framing

All integers are unsigned, explicitly little-endian. A frame consists of this
**40-byte header** followed immediately by its payload. Native C structure
layout is never transmitted. The implementation uses fixed-size buffers.

| Byte offset | Width | Meaning |
|---|---|---|
| 0 | 4 | Magic `0x41584943` (wire bytes `43 49 58 41`) |
| 4 | 2 | Major version, exactly 1 |
| 6 | 2 | Minor version, exactly 0 |
| 8 | 2 | Kind; bit 15 denotes a response |
| 10 | 2 | Reserved, must be zero |
| 12 | 4 | Response status; zero in requests/events |
| 16 | 8 | Nonzero request/event ID |
| 24 | 8 | Guest physical address, MMIO offset, or zero for control/events |
| 32 | 4 | Payload length, at most 1024 bytes in this version |
| 36 | 4 | Flags, must be zero |

Each channel's initiating side assigns monotonically increasing IDs, including
IRQ events. HELLO uses ID 1. Responses preserve kind (with bit 15 set), ID and
address. IDs do not restart on RESET. Exhausting the ID space requires a new
session; wraparound is not supported. Only one command and one reverse DMA RPC
are outstanding at a time. No reordering or reconnection is supported.

Invalid magic/version/reserved fields, oversized frames, unexpected kinds,
response correlation errors and mid-frame disconnects terminate the session.
An invalid header is never trusted sufficiently to allocate its payload or
construct a successful response. TCP-style stream fragmentation is supported;
socket read/write calls do not establish frame boundaries.

## Channels and messages

Channel A (`command.sock`) is initiated by QEMU. Channel B (`reverse.sock`) is
initiated by the simulator after its HELLO exchange with QEMU. Both connections
are established by QEMU; "initiated" here describes request ownership.

| Kind | Name | Channel | Request/event payload | Successful response payload |
|---|---|---|---|---|
| 1 | HELLO | Both, QEMU first | 32-byte capabilities | Identical capabilities |
| 2 | MMIO_READ | A | Empty | One 32-bit word |
| 3 | MMIO_WRITE | A | One 32-bit word | Empty |
| 4 | DMA_READ | B | 32-bit byte count | Exactly that many bytes |
| 5 | DMA_WRITE | B | Count, data bytes, enable bytes | Empty |
| 6 | IRQ_LEVEL | B | 32-bit level, 0 or 1 | No response |
| 7 | RESET | A | Empty | Empty, after reset completes |
| 8 | ERROR | Reserved | Not emitted in M1 | Not supported in M1 |
| 9 | GOODBYE | A | Empty | Empty, then QEMU closes connections |

Error responses contain no data. Status values are 0 OK, 1 alignment, 2 range,
3 unsupported access, 4 timeout, 5 protocol error, 6 simulator stopped, and
7 AXI/memory bus error. Timeout/stopped statuses are reserved for future peers:
the implementation closes a failed transport rather than fabricating a
transaction response. It logs the host errno separately.

HELLO payload offsets: 0 MMIO width (32 bits), 4 AXI data width (64 bits),
8 maximum AXI burst (16 beats), 12 IRQ count (1), 16 nominal clock
(100,000,000 Hz), 20 maximum reverse RPC bytes (128), and 24 a 64-bit session
token. The runner chooses a fresh nonzero token and passes it to both processes.
Both channels must match all capabilities and the token. This prevents accidental
cross-pairing, not malicious access by the same user. The 128-byte backend limit
is independently tested. M2 coalesces each 1–16-beat burst into one RPC; the
transport format remains 1.0, but old peers advertising one beat are rejected.
Rebuild both peers together after this capability change.

MMIO is aligned 32-bit access within a 4096-byte aperture. Offset, not the
physical MMIO address, goes over A. The transactor drives actual AXI-Lite pins;
the QEMU bridge has no accelerator register model or image operation.
An empty `AX_BUS_ERROR` MMIO reply maps to QEMU `MEMTX_ERROR` without closing
the session; an illegal register access must not poison subsequent valid access.
Other MMIO error statuses, nonempty error replies and wrong success lengths are
protocol failures. QTest ignores `MemTxResult`, so its probes verify bridge
survival and all-ones error data, not a guest CPU's synchronous-abort handler.

DMA count must be nonzero, no more than 128, and a multiple of 8; addresses must
be 8-byte aligned and the entire range must not overflow. DMA_WRITE payload is
`u32 count`, `u8 data[count]`, `u8 enables[count]`; each enable is exactly 0 or 1.
QEMU validates the entire region before touching memory, including disabled
lanes. It rejects holes, ROM, read-only mappings, MMIO and RAM-device regions.
It writes contiguous enabled runs, never read-modify-writes surrounding bytes.
All AddressSpace transaction results are checked. The simulator maps range and
alignment failures to AXI DECERR, other returned failures to SLVERR, and only
acknowledges a write after the reverse RPC finishes. Read errors still generate
the requested number of R beats. Burst legality and 4-KiB splitting are enforced
at the AXI transactor/core, not by interpreting RPCs as AXI pins inside QEMU.

## Progress, reset and shutdown

QEMU holds its BQL while forwarding a guest MMIO. Its bounded I/O helper polls
both channels and services B before waiting further for A, including during
partial-frame reads and output backpressure. Outside an MMIO callback, a QEMU
main-loop fd callback services B under the same BQL. That callback checks current
readiness because a queued notification may already have been consumed by the
MMIO pump. No application thread calls QEMU memory or IRQ APIs.

QEMU's default transaction deadline is 2000 ms, configurable with
`timeout-ms=1..10000`. A single absolute monotonic deadline covers a complete
request/reply and nested reverse work. EINTR does not restart it. The simulator
uses 5-second command/DMA bounds, a 5-second spike or 30-second framefx job
bound, and a 30-second initial accept bound. The job watchdog starts on observed
RTL/fabric activity, not a particular CSR write; invalid or repeated writes
cannot refresh the active-job deadline.
The outer runner bounds QEMU and reaps process groups. These are host-time
safety limits, not simulated RTL timing.

RESET drives four synchronous RTL reset edges, drops transactor queues and
deasserts IRQ before acknowledging. QEMU invokes it during startup and system
reset. It does not undo already committed memory writes. Local queue/RTL reset
is tested by `make rtl-test`; interrupting an in-flight blocking socket RPC is
not supported. GOODBYE also resets the simulator before its reply. The simulator keeps
B open until QEMU consumes the A reply and closes A, avoiding a reverse-EOF
race during normal shutdown. Process exit during DT dumping is also accepted
when the simulator is idle; it is not treated as a completed RTL job.

Transport failure latches the QEMU bridge off, deasserts IRQ, unregisters B and
shuts down both sockets. Subsequent reads return `0xffffffff`, writes are
ignored, and no further DMA is serviced. These terminal sentinel accesses return
`MEMTX_OK` deliberately, preserving the Linux driver's offline detection path;
they are distinct from a live RTL bus error. Recover by restarting the complete session, **not** by
reusing DMA allocations or reconnecting a peer. Migration/snapshots are blocked.

## Validation and limits

`make test` independently checks golden header bytes, strict decoding, reply
correlation, fragmented reads/writes, backpressure, EOF, EINTR, deadlines and
reverse progress before a command reply. `make spike-test` checks real Linux/RTL
concurrency, plus a separately labelled synthetic-peer bridge safety probe.
See [M1 guide](../docs/m1-spike.md) for scope and evidence.
`make stress-test` adds reply/IRQ/ID corruption, slow headers, reverse IRQ floods
against the same absolute deadline, no post-fault reset/reconnect, and 384 invalid
MMIO operations against the actual framefx RTL followed by valid accesses.

The shared burst transactor supports aligned full-width INCR only, one read
and one write outstanding, with no IDs or reordering. There is no IOMMU integration, cache-coherency model,
hot-unplug or recovery from a reset concurrent with an
in-flight reverse RPC. The current payload cap is fixed at protocol version 1.0;
changing it or capabilities requires coordinated peers and new regression runs.
The stress target exercises one and four TCG vCPUs; this does not establish
arbitrary-vCPU scalability or cycle synchronization with the guest.
