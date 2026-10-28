# Platform contract and feasibility

This is a functional soft-IP and Linux-driver verification platform, not a
cycle-synchronized SoC simulator or a universal AXI compliance environment.
The useful invariant is real execution of the same synthesizable core, with
pin-level control/DMA transactions and an independent byte-level scoreboard.
Framefx is the first production workload; the XOR spike is a separate
deadlock/concurrency fixture, not an alternate implementation of framefx.

## Supported envelope

| Layer | Contract | Not established |
|---|---|---|
| RTL | One synchronous clock/reset domain; real Verilator model, assertions, trace; separate V1 routed timing and hardware gates | Four-state/X behavior, CDC, analog behavior, arbitrary-IP physical acceptance |
| AXI-Lite | 32-bit, aligned accesses, independent AW/W, one outstanding command, configurable standalone aperture | Multiple outstanding requests, narrow/unaligned CPU MMIO |
| DMA fabric | Aligned full-width INCR, 1–16 beats, independent read/write, W-before-AW, stable VALID/payload under stalls | IDs, reordering, exclusives/atomics, WRAP/FIXED/narrow transfers, AXI Stream |
| Data width | Standalone fabric instantiated at 32 and 64 bits; current socket profile is 64 bits | Automatic width negotiation/adaptation |
| Memory | Simulation: entire-range validation, byte enables, checked backend errors, guest RAM only; V1: kernel DMA API over non-coherent HP0 | IOMMU/IOVA translation, simulated cache/DDR contention, hardware DMA isolation |
| Linux | Fixed ABI, exclusive open description, DMA API allocations, IRQ-driven completion, one/four-vCPU stress, ARM32 compat fixture | Multi-client queues, arbitrary 32-bit applications, supported hot removal |
| Failure | Bounded transport deadlines, local RTL/fabric reset, error recovery versus terminal quarantine | In-flight RPC cancellation, reconnect, migration/snapshot, safe hardware abort |

The transactors reject unsupported shapes; silently accepting an unsupported
AXI feature would invalidate a test. Their cycle timeouts are test-environment
bounds, not proof that arbitrary legal bus backpressure eventually terminates.
R/B responses are checked at VALID, including while READY is low, rather than
only when accepted. Burst payloads must remain stable through stalls.

## Time and memory semantics

Each `ClockedModel::step` samples pins at the rising edge and advances the
fabric. The 100 MHz HELLO/VCD setting is a nominal RTL clock. It is **not** tied
to QEMU virtual time, a guest timer, wall time or a physical FPGA clock.
Standalone runs replay bus history and counters exactly with a recorded seed.
Linux scheduling, IRQ timing and MMIO ordering need not replay cycle-for-cycle.

The socket runner clocks continuously while the DUT's `debug_busy`, the memory
fabric or AXI-Lite master is active. When quiescent, its command poll can wait
20 ms before another cycle. A free-running timer, periodic DMA source or other
autonomous IP must assert an appropriate activity signal or use a deliberately
always-clocked runner. Porting without addressing this assumption can create
false liveness or performance conclusions. Job deadlines now follow activity,
not a framefx-specific control offset.

The memory backend snapshots a complete read burst when AR is accepted and
commits a complete write after AW and all W beats, before B. This saves RPCs but
is not per-beat concurrent-memory timing. CPU writes during DMA, overlapping
traffic from additional masters and visibility between individual beats are
outside the ownership contract. Reset discards pending local queues but cannot
undo a backend write that already committed. RLAST corruption and backend bus
errors are tested faults, not a model of every physical interconnect failure.

## Concurrency and safety

QEMU's BQL serializes bridge MMIO, reverse callbacks and IRQ changes. Waiting
for channel A explicitly pumps channel B, so deferred START responses do not
deadlock DMA. A single absolute deadline also bounds partial headers and an IRQ
flood. Four-vCPU TCG stress exercises multiple guest execution contexts, but
does not remove BQL stalls or prove throughput scalability. No extra socket
thread calls AddressSpace/IRQ APIs outside QEMU's execution context.

An AXI bus error is not a broken transport. Empty MMIO `AX_BUS_ERROR` replies
return `MEMTX_ERROR` and preserve the session. Malformed correlation/framing or
timeout permanently disconnects it. The failed-session all-ones sentinel is
intentional so the driver can fail closed without depending on a CPU-abort
handler. That terminal behavior is a simulation policy, not a physical-bus model.

The guest and RTL process are trusted test workloads. RAM-range validation
prevents DMA into host pointers or guest MMIO, but does **not** restrict an IP
to the driver's two allocations: a faulty core can corrupt other guest RAM.
An isolated scratch 9p share is writable by guest root. Neither Unix socket
permissions nor a session token make this an adversarial-IP security sandbox.
Use disposable guests and never share the repository or home directory.

The driver publishes buffers only after successful allocation copyout, using
a separate mapping lock to avoid `ioctl → mmap_lock → mmap → ioctl-lock`
inversion. File/VMA references own lifetime. Because ABI 1 cannot abort DMA,
unproven idle means bounded retention and a pinned module, not unsafe freeing.
An external reset/isolation design is essential before physical fault recovery.

Control-plane success is checked too: a transport failure after an accepted
START is an uncertain execution, not a safe-to-retry rejection. The driver
latches offline on failed identity checks or inconsistent completion telemetry,
and disables a malformed level IRQ rather than spinning. It releases memory
only after independently re-establishing DMA idle; otherwise quarantine remains
mandatory. These checks rely on the core's declared idle semantics. A malicious
or broken core that lies consistently about idle/counters is not contained by
this driver or the whole-guest-RAM bridge.

## Adding another soft IP

1. Freeze its register ABI, reset semantics, memory ownership, error policy and
   supported AXI subset. Decide whether a single clock/activity indication is
   sufficient; do not assume framefx's idle behavior for autonomous hardware.
2. Connect a Verilated top's `s_*` AXI-Lite, `m_*` DMA, `clk`, `reset_n`, `irq`
   and activity pins to `sim/axi/model.hpp`, directly or through a thin pin
   adapter. Keep the functional implementation in RTL, not the backend.
3. Begin with `axi::Ram`: compare the entire memory image, guards, burst
   sequence and counters against an independent reference. Cover ready/valid
   stalls, boundaries, error responses, reset phases and reproducible replay.
4. Integrate the same top with `axi::SocketMemory`. The existing socket executable
   has two compile-time profiles; it is not a plugin loader. Add an explicit
   profile/build target rather than pretending arbitrary IP is auto-discovered.
   New widths or capabilities require coordinated HELLO changes and tests.
5. Add the QEMU DT binding/identity and an appropriate Linux driver/UAPI. QEMU
   transports registers; it must not emulate the new core's algorithm. The
   framefx driver is reusable unchanged only for its own frozen register ABI.
6. Require all existing gates plus new full-RAM, Linux, malformed-peer and
   debug-kernel stress gates. Preserve source/binary hashes and failed-case
   traces. Do not replace independent expectations with values from the DUT.

## Feasibility assessment and next gates

The approach is useful now for control-plane behavior, DMA correctness,
IRQ/software ordering, Linux buffer lifetime and deterministic local RTL
regressions. Lockdep finding a real driver defect and an adversarial peer
finding a bus-error handling defect demonstrate why layered validation matters;
more random seeds alone would not have covered either defect.

The V1 integration applies the pinned board preset, assigns real control/DDR/IRQ
paths and closes routed 100-MHz timing. The same core and driver have passed
physical CSR, non-coherent HP0 DMA, interrupt and all-filter checks. These are
bounded observations for this design, not acceptance of arbitrary IP or a
finished SD deployment. Storage still maps to distributed RAM/registers rather
than block RAM. Physical loading remains a fresh-boot, supervised operation;
see [ADR 0002](adr/0002-ultra96-volatile-pl.md).

Do not optimize based on QEMU wall time: IPC/logging and scheduling dominate.
Physical job measurements exclude host export/display and are not a video
throughput benchmark. The next deployment gates are tracked in
[current status](progress.md), rather than inferred from this contract.
Multi-clock, IOMMU, multiple outstanding transactions and streaming IP are
deliberate scope changes requiring new contracts/tests. Formal properties or
synthesized checkers would complement, not replace, Linux and hardware testing.
