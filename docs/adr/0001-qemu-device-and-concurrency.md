# ADR-0001: Generic SysBus bridge and concurrency spike

Date: 2026-10-15. Status: **ACCEPTED for the tested M1 configuration**.
The real Linux/RTL concurrency gate passes. Stock boot alone was not sufficient;
the acceptance evidence and limitations are recorded below.

## Decision

Use pinned upstream QEMU 10.2.4 `virt-10.2`, TCG, one Cortex-A53 vCPU,
Buildroot/Linux and a direct C++ Verilator harness. Implement a small SysBus
bridge and two local Unix stream sockets. Preserve the same eventual
synthesizable framefx core, DT compatible, driver source and UAPI for Ultra96 V1.

Xilinx Remote-Port is a useful alternative: its reference library already has
memory-master, memory-slave and signal adapters, and its demo includes reverse
bus mastering. However, that documented path introduces SystemC and Xilinx QEMU
machine integration. No tested reuse path for our generic pinned `virt` guest
has yet been established. Keep both repositories pinned for reading; do not
claim that merely fetching them demonstrates interoperability.

## What inspection of the pinned QEMU source establishes

- `hw/arm/virt.c:base_memmap` reserves `VIRT_PLATFORM_BUS` at `0x0c000000`, size
  `0x02000000`. Its interrupt map starts platform SPIs at 112. These are facts
  about this QEMU revision, not addresses to hardcode in a driver or board DT.
- `create_platform_bus()` creates the MMIO container and connects its IRQs.
  `virt_machine_device_plug_cb()` calls `platform_bus_link_device()` for allowed
  dynamic SysBus types, allocating MMIO/IRQ resources.
- `virt_machine_class_init()` explicitly allowlists dynamic types.
  `hw/core/sysbus-fdt.c:bindings` separately selects DT node constructors;
  unknown types are rejected. Arbitrary devices do **not** get a usable DT node
  automatically. A bridge patch must cover both hooks.
- `virt_machine_done()` emits platform-bus nodes before loading the generated DT.
  The bus has one address and one size cell, with `ranges` translating to parent
  addresses. A bridge node must use the allocated bus-relative offset and SPI,
  rather than an invented root-level address.
- `system/physmem.c:prepare_mmio_access()` acquires the BQL for ordinary MMIO.
  A synchronous device callback cannot wait on a service that needs the same
  BQL while making no progress itself.

Primary references: the [pinned virt source](https://github.com/qemu/qemu/blob/v10.2.4/hw/arm/virt.c),
[DT helper](https://github.com/qemu/qemu/blob/v10.2.4/hw/core/sysbus-fdt.c),
[memory implementation](https://github.com/qemu/qemu/blob/v10.2.4/system/physmem.c),
and [IOThread documentation](https://github.com/qemu/qemu/blob/v10.2.4/docs/devel/multiple-iothreads.rst).
Local copies live under `third_party/qemu-10.2.4/`.

## Tested concurrency design

Use the registered QEMU execution contexts and BQL as the initial serialization
boundary. Channel A carries ordered MMIO commands/replies. Channel B carries
reverse DMA and level IRQ notifications. While waiting synchronously for an A
reply, the MMIO callback polls **both** channels with one absolute deadline and
services B messages under its existing BQL ownership. This explicitly allows a
DMA read to finish before the RTL returns the MMIO write response.

When no MMIO is outstanding, the QEMU main-loop fd handler services B. BQL
ownership prevents these two contexts from consuming B concurrently. No raw
socket worker invokes AddressSpace or IRQ APIs. No generic nested main-loop
iteration is used: the callback only services this bridge's bounded protocol
work. Partial frames and output backpressure must also remain bounded.

This is a small, deliberately synchronous first design: long MMIO waits can
delay other BQL users until the deadline. The original M1 result was not evidence
of high throughput or multi-vCPU operation. The later stress target exercises
four TCG vCPUs with a debug kernel but does not establish scalability. Revisit a dedicated QEMU IOThread or an
in-process adapter if the accelerator's throughput requirements make the bounded
BQL stalls unacceptable. Two sockets compiling was not the acceptance criterion.

DMA must first validate overflow, maximum length, all mapped segments, RAM type,
ROM/read-only status and permissions. Reject MMIO, including recursion into the
bridge, and RAM-device regions. Use the AddressSpace APIs, appropriate RCU/lifetime
rules and checked `MemTxResult`, never casts from guest addresses to host pointers.
Masked writes operate on runs of enabled bytes; they must not read-modify-write
adjacent bytes. IRQ is a persistent level, updated under a valid QEMU context.

## Acceptance evidence

1. Device is allocated collision-free, appears in the dumped DT, and binds to an
   in-guest OF platform test driver by compatible.
2. The driver allocates DMA-safe memory and reports actual source/destination bytes.
3. A barebones clocked AXI-Lite RTL target issues reverse DMA before completing
   a START write response. Repeated tests finish under delayed/fragmented traffic.
4. Standalone reverse traffic continues while Linux waits for a level IRQ.
5. Disconnects, partial frames, malformed lengths, timeouts and reset terminate
   safely; no successful DMA response is fabricated on failure.
6. Logs establish thread/BQL ownership, ordering, memory visibility and IRQ
   acknowledgement. Migration/snapshot support is explicitly blocked in v1.

All six checks have runtime evidence in `artifacts/latest-spike.json` and its
referenced run directory. The initial full suite passed at
`artifacts/spike-20261015T234408-322069Z/`; the expanded independent bridge safety
probe also passed in `artifacts/spike-20261015T234657-237682Z/`. Subsequent runs add
explicit host thread IDs and retain the same checks. The latest report verifies
source/binary hashes before calling the current M1 inputs accepted.

The original three healthy Linux boots each completed 36 jobs and 36 IRQ acknowledgements,
including 18 deferred START responses, 18 early responses, masked IRQ delivery,
four byte-mask patterns and four expected DMA range errors. Logs establish that
the first reverse read completes before START's response, and that DMA also
progresses in the main loop while the guest is waiting. Traffic is repeated with
7-byte and 1-byte send fragmentation. The RTL performs the XOR transform, not
the bridge or simulator host code. Full-buffer comparisons include guard bytes.

Disconnect, stalled peer, oversized frame and truncated frame boots fail closed
and power down within the runner's bound. Independent C/Python transport tests
cover header bytes, correlation, EINTR, absolute deadlines and backpressure.
The synthetic-peer probe separately checks QEMU's real RAM effects, protection
checks, migration blocker and healthy QMP reset. Startup/shutdown reset also
clocks the actual RTL. Reset-abort during an in-flight reverse RPC and recovery
after a transport fault are **not** supported or claimed.

Two runtime discoveries affected the final design:

- A main-loop fd callback can be queued before the MMIO pump drains B. Recheck
  readiness before reading in that callback; otherwise it blocks on an empty
  stream while holding the BQL and prevents Linux from taking the IRQ.
- On GOODBYE, immediate closure of B can race ahead of QEMU consuming the final
  A response. Keep B open until QEMU has read the reply and closed A. Failures
  on other operations remain terminal; no blanket EOF suppression was added.

The FDT hook uses the dynamic platform offset/IRQ and a nonempty identity
`dma-ranges` derived from actual main RAM. This avoids empty-range cell-width
warnings on the platform bus. The current bridge restricts main RAM below 4 GiB;
the driver still uses DMA APIs and 64-bit AXI addresses. Virtual `dma-coherent`
must not be copied blindly to the Ultra96 hardware DT.

See [M1 guide](../m1-spike.md) and [wire protocol](../../transport/protocol.md).

## M2 extension

The socket peer now uses the same pin-level models and burst core as the
standalone RAM harness. Each legal 1–16-beat burst becomes one reverse RPC;
the core splits at both address streams' 4-KiB boundaries. HELLO now requires
the 16-beat capability, so both peers must be rebuilt together. No QEMU
concurrency architecture or memory-access rule changed for this extension.

The guest adds four multi-burst cases per seed: 40 jobs/IRQs, 61 read RPCs and
59 write RPCs. Standalone tests cover local reset of partially accepted channels
and pending responses. They do not remove the in-flight socket reset limit.
See [M2 guide](../m2-fabric.md); M3–M4 have separate acceptance evidence in
[current status](../progress.md).

## Post-M4 hardening

The platform stress target adds one-/four-vCPU Linux contention, lockdep and
DMA-API debugging, adversarial protocol peers and randomized RTL replay.
Finite BQL waits remain the design boundary. Slow headers and a continuous
reverse IRQ stream must not extend a command's absolute deadline; a failed
session must not revive on QMP reset.

Valid empty AXI-Lite bus-error replies now propagate `MEMTX_ERROR` without
disconnecting. The earlier treatment of every nonzero MMIO status as a terminal
protocol error was incorrect; both a synthetic reproducer and actual RTL illegal
register accesses cover the correction. Terminal transport failures retain the
all-ones/ignored-write policy used by the driver's offline path.

The debug-kernel pass found and corrected an API-mutex/mmap-lock inversion and
an unnecessarily short active-close budget. These are driver lifetime/locking
changes, not a replacement QEMU threading model. See [verification](../verification.md),
[M4](../m4-linux.md) and the [platform contract](../architecture.md).
