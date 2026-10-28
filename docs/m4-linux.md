# M4 — Linux framefx integration

The production path is `/dev/framefx0` → platform driver → QEMU AXI-Lite
bridge → the synthesizable `framefx_core` → AXI DMA → guest coherent memory.
The simulator uses the same clocked AXI fabric as M1–M3; it does not compute
image results in software. C and Python references only validate the output.

## Run

```
make demo-sim JOBS=8 BOOT_TIMEOUT=180
make report
```

This builds QEMU with virtio-9p, the pinned Linux/Buildroot guest, both the
module and CLI, and the real-core socket simulator. A 9p share exports only a
new, per-run `artifacts/demo-*/seed-*/export/` scratch directory, never the
repository or home directory. Guest root is trusted; the QEMU process runs as
the invoking host user (`security_model=none`). There is no guest network.

`axi-cosim,spike=off` selects the production device-tree binding. The default
remains the M1 experimental spike for backwards-compatible transport tests.
Both drivers explicitly reject the other ABI. MMIO addresses and interrupts
come from the generated DT; the production driver contains no QEMU addresses.
The QEMU DT's `dma-coherent` is a virtual-machine property, not a prescription
for a physical Ultra96 PS–PL port.

The guest runs all four modes at 320×240, 640×480, 65×9 (three repeats),
1024×1024, and 3×3 after module unload/reload. Source and destination strides
differ, padding uses distinct sentinels, and allocations include a guard page.
Each job saves packed input/output PGM files, both complete coherent-buffer
snapshots, and counters/timing JSON. The host independently regenerates input,
recomputes each image, checks every allocation byte including padding/guards,
and records SHA256 hashes. Timing is emulation wall time, not FPGA throughput.
Binary export copies through an ordinary-memory staging buffer: 9p's zero-copy
write path cannot pin the coherent DMA VMA directly. This does not alter the
DMA allocation or the image comparison.

Standalone guest invocation, after loading the module and creating an output
directory:

```
framefx-test --width 640 --height 480 --seed 42 --mode sobel --repeat 1 \
  --threshold 127 --headless --selftest --output /evidence/manual
```

Modes: `copy`, `invert`, `threshold`, `sobel`, `all` (default). `--repeat` repeats
the selected mode or complete four-mode set. No display server is required.
`--timeout-ms` selects a bounded 1–30000 ms WAIT (default 30000); terminal-fault
boots deliberately use 5000 ms.

## UAPI version 1

Canonical header: `linux/uapi/framefx.h`. All structures use fixed-width fields
and explicitly eight-byte-aligned 64-bit members. Reserved fields must be zero;
the entire structure should be zero-initialized before a request. ioctl numbers
and structure offsets are compile-tested with both x86 32- and 64-bit ABIs and
cross-built for AArch64. `make hardening-test` also runs a freestanding 32-bit ARM
process on the AArch64 guest with one and four vCPUs. `compat_ptr_ioctl` forwards
the identical layout. Runtime tests cover all ioctls, mmap2 offsets, pointer
faults, non-executable mappings, high job-ID bits, all four image modes, full
allocation guards, retained VMA ownership and final-unmap reclamation. This is
not a claim of arbitrary 32-bit libc/application compatibility.

| Operation | Size | Contract |
|---|---:|---|
| `QUERY_CAPS` | 32 | ABI/hardware version, modes, dimension/alignment/allocation limits |
| `ALLOC_BUFS` | 32 | One source/destination allocation pair per open; capacities in bytes; returns page-rounded capacities and mmap offsets |
| `SUBMIT` | 40 | Dimensions, strides, mode, threshold; returns monotonic job ID; never accepts DMA/physical addresses |
| `WAIT` | 48 | Matching job ID and timeout 1–30000 ms; returns status/error/cycles/read bytes/written bytes/cumulative IRQ count |
| `RESET` | none | Idle-only soft reset, argument zero; invalidates the last result, retains allocations |

Only one open file description owns a device. Allocation is capped at 2 MiB
per buffer. Map the entire returned capacity using `MAP_SHARED`, offset zero
for source or the returned destination offset. Private, executable, partial,
oversized, and unknown-offset mappings are rejected. Fork does not inherit
these mappings; executable remapping is disallowed. Do not modify either
allocation between SUBMIT and completion. Exclusive ownership protects address
isolation, not against an owner's own concurrent memory writes.

The driver explicitly negotiates a 64-bit DMA mask with 32-bit fallback and
uses `dma_alloc_coherent` / `dma_mmap_coherent`; it never uses `virt_to_phys`.
All geometry and allocation bounds are validated before touching START. One
unconsumed result blocks another SUBMIT, even if hardware already finished.
Successful WAIT consumes that result; another WAIT can retrieve the same
cached result until the next SUBMIT or RESET. A DMA error returns `EIO` with
the hardware result filled in. Interrupted or timed-out waits do not consume
the result or abort DMA. Stale IDs return `ESTALE`.

Capability queries require the supported hardware version/capability word.
Configuration and START writes are followed by an identity read; RESET must
also retain a responsive device before success is reported. Transport loss or
invalid telemetry latches the device offline. The detecting ioctl returns
`EIO`; subsequent operations/open return `ENODEV`. An asynchronous IRQ fault can
already have latched offline before WAIT enters, so WAIT may return `ENODEV`.
A failed SUBMIT is **not** proof that START did not execute: job-ID copyout and
START can precede loss of the response. Never recycle DMA memory on that basis.

Completion validates reserved state/IRQ bits, terminal state, error-code range,
error-bit agreement and byte counts against the submitted geometry. A successful
completion requires exact read/write counts; a hardware error permits bounded
partial progress. Invalid IRQ telemetry disables the Linux IRQ without waiting
inside the handler and wakes waiters offline, preventing a level-triggered IRQ
storm. Cycle-counter halves are not individually treated as fault sentinels:
`0xffffffff` can be a legitimate counter value. These checks detect inconsistent
telemetry, not arbitrary internally consistent lies from a faulty core.

The IRQ handler snapshots terminal status and low-latched/high cycle counters,
accounts for interrupts, acknowledges W1C status, orders coherent reads, and
wakes the completion. The API mutex serializes operations; IRQ handling uses a
separate spinlock. WAIT sleeps interruptibly with a bounded timeout, not busy
polling. Close may wait up to 30 seconds, matching the maximum WAIT budget,
for positively verified DMA idle. Slow valid maximum-size jobs must not be
quarantined merely because close previously had a shorter five-second budget.
The lifecycle executable also checks active RESET rejection, a one-millisecond
WAIT timeout, signal-interrupted WAIT, fork mapping exclusion, executable-remap
rejection, unload refusal while mapped, and close/final-unmap during active DMA.
The guest compares CMA allocation/release counter deltas across the complete
normal suite. `CmaFree` alone is not a leak check: movable non-DMA allocations
can temporarily occupy CMA pageblocks.

The buffer-publication mutex is separate from the API mutex: Linux enters
`mmap` with `mmap_lock` held, while ioctl user copies can fault and acquire that
lock. Taking the API mutex in both paths created a real lock-order inversion
found by lockdep. Allocation now builds a private pair, copies the result to
userspace, then publishes both buffers under the buffer mutex. No user copies
run under that mutex. Failed copyout frees only unpublished buffers; an alias
mapping cannot observe an allocation that will be rolled back. Published
buffers stay immutable until final file/VMA release, which cannot overlap a
file-referenced ioctl or mmap. No new owner can enter until release completes.

`make stress-test` checks this with lockdep/DMA-API debugging, four pinned
contenders on one and four vCPUs, repeated allocation owners, duplicate fds,
additional mappings, split VMAs, copyout faults and SIGKILL during a large DMA
job. Two WAIT callers may retrieve the same cached result; the API mutex still
serializes ioctls, so this is not a multi-queue submission interface.

## Buffer lifetime and fail-closed behavior

Mapped VMAs retain the file reference and its module reference. Closing a file
descriptor does **not** free its mappings or surrender ownership. Only the last
VMA/file reference can release the allocation; the regression closes the fd,
accesses both maps, rejects a second owner, then unmaps each buffer and proves
reopen succeeds. Normal completion/close allows module unload and reload.

There is no safe hardware abort in ABI 1. If DMA idle cannot be positively
proven (disconnect, dead bridge, or close timeout), the driver deliberately
quarantines the buffers, device reference, and module reference. It refuses
new opens and emits `FRAMEFX_QUARANTINE`. This is a bounded, at-most-two-buffer
retention per failed device, **not** successful reclamation or transparent
recovery. Restart the VM; a physical deployment requires an externally proven
PL reset before any equivalent recovery can be designed. Freeing memory while
an unknown DMA master might still write would be unsafe.

Sysfs bind/unbind attributes are suppressed for this static platform device.
Module unload is protected by open/mapped file references and the quarantine
pin. Forced platform removal is fail-closed; hot removal is not a supported or
validated board workflow. These simulator tests do not establish physical
reset, cache coherency, IOMMU behavior, IRQ wiring or board timing; the separate
V1 gates establish only their recorded hardware scope.

Four terminal transport cases (disconnect, stall, malformed header, truncated
header), injected at the eleventh DMA request after partial output, must report
failure, retain unsafe-to-free allocations, refuse unload
and reopen, perform no further bridge DMA, and still shut the guest down.
Evidence includes exact commands, DTB/DTS, resolved configs, source/binary
hashes, simulator/guest logs and per-case JSON. Failed runs remain for diagnosis.
CI packages demo directories in `demo-evidence.tar.gz`, preserving fault labels
and keeping the artifact upload compact; local evidence stays directly browsable.
Two additional boots inject a one-shot AXI SLVERR at the seventh read or write
burst. They check partial-progress counters and error codes, then resubmit and
verify complete images without RESET. These are explicit simulator memory-backend
faults, not claims about physical bus-error injection.

Primary API references: Linux [DMA mapping guide](https://kernel.org/doc/html/next/core-api/dma-api-howto.html)
and [ioctl layout/compatibility documentation](https://cdn.kernel.org/doc/html/latest/driver-api/ioctl.html).
Implementation is compiled and runtime-tested against the pinned 6.12.109
simulation kernel, not an assumed latest-kernel API. The same source also
compiles natively against the Ultra96's 5.15.19 vendor headers. Small versioned
branches handle [VMA flag helpers introduced in 6.3](https://raw.githubusercontent.com/torvalds/linux/v6.3/include/linux/mm.h)
and the [void platform remove callback in 6.11](https://raw.githubusercontent.com/torvalds/linux/v6.11/include/linux/platform_device.h),
retaining the existing protections and cleanup behavior. That board build has
also passed CSR, DMA/IRQ and all-filter checks against the real core, separately
from simulation. See [physical evidence](bringup-ultra96.md#physical-dma-and-all-filter-gates-passed).

The V1 preparation adds an optional clock reference acquired/enabled before
probe's first MMIO and released only with the final device reference. Quarantine
therefore retains the clock as well as unsafe DMA buffers; it is not tied to
automatic platform devres removal. The QEMU DT needs no clock property.
`framefx-probe` performs 64 stable capability/version queries without allocation
or SUBMIT and is included in the normal simulation boot before image tests.
The V1 overlay constrains DMA to low 2-GiB DDR through `dma-ranges`; it does not
assert cache coherence for the HP0 path. See [PL integration](bringup-ultra96.md#headless-pl-integration).
